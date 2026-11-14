import numpy as np
import pytest

from cellscan.sdr import lte_sync as ls


def test_sequences_are_valid():
    for nid2 in range(3):
        p = ls.pss_freq(nid2)
        assert p.shape == (62,) and np.allclose(np.abs(p), 1)
    # PSS roots are nearly orthogonal
    assert abs(np.vdot(ls.pss_freq(0), ls.pss_freq(1))) / 62 < 0.2
    s = ls.sss_freq(100, 1, 0)
    assert set(np.unique(s)) <= {-1.0, 1.0}
    # subframe 0 and 5 sequences differ; all 168*2 sequences per nid2 are distinct
    seqs = {tuple(ls.sss_freq(n1, 2, sf)) for n1 in range(168) for sf in (0, 5)}
    assert len(seqs) == 336
    # TS 36.211 Table 6.11.2.1-1: groups with m1 - m0 = 1 (N_ID_1 0..29), 2 (30..58), 3 (59..86) ... 7 (165..167)
    assert [ls._m0_m1(n) for n in (0, 29, 30, 58, 59, 165, 167)] == [(0, 1), (29, 30), (0, 2), (28, 30), (0, 3), (0, 7), (2, 9)]
    assert len({ls._m0_m1(n) for n in range(168)}) == 168


@pytest.mark.parametrize("pci,offset,cfo,snr", [
    (0, 0, 0, 10), (1, 5000, 3000, 5), (2, 12345, -7000, 3), (301, 777, 12_000, 6), (503, 18_000, -15_000, 4),
])
def test_single_cell(pci, offset, cfo, snr):
    x = ls.synthesize([pci], duration_s=0.03, snr_db=snr, cfo_hz=cfo, frame_offset=offset, seed=pci)
    cells = ls.cell_search(x)
    assert cells, "no cell found"
    c = cells[0]
    assert c.pci == pci
    assert min(abs(c.frame_start - offset), ls.FRAME - abs(c.frame_start - offset)) <= 2
    assert abs(c.cfo_hz - cfo) < 500
    assert c.snr_db > snr - 6


def test_two_unsynchronised_cells():
    x = ls.synthesize([120, 301], gains_db=[0, -6], duration_s=0.04, snr_db=10, seed=3)
    # give the second cell a different timing by shifting a separately synthesised copy
    a = ls.synthesize([120], duration_s=0.04, snr_db=30, seed=1)
    b = ls.synthesize([301], duration_s=0.04, snr_db=30, frame_offset=4000, seed=2)
    x = a + 0.5 * b
    pcis = {c.pci for c in ls.cell_search(x)}
    assert {120, 301} <= pcis


def test_noise_only_finds_nothing():
    rng = np.random.default_rng(7)
    noise = (rng.standard_normal(57600) + 1j * rng.standard_normal(57600)).astype(np.complex64)
    assert ls.cell_search(noise) == []


def test_short_capture_rejected():
    with pytest.raises(ValueError):
        ls.cell_search(np.zeros(1000, dtype=np.complex64))


def rtl_capture(pci, ppm, fc, duration, snr, seed):
    """Crystal `ppm` slow: the signal appears at +ppm*fc and the sampler takes fewer samples per second."""
    x = ls.synthesize([pci], duration_s=duration * 1.001, snr_db=snr, cfo_hz=ppm * 1e-6 * fc, seed=seed)
    n = np.arange(len(x))
    t = n * (1 + ppm * 1e-6)
    t = t[t <= len(x) - 1][: int(duration * ls.FS)]
    return (np.interp(t, n, x.real) + 1j * np.interp(t, n, x.imag)).astype(np.complex64)


def test_sample_clock_drift_compensation():
    # Real case: RTL-SDR ~76 ppm off (+61 kHz at 806 MHz), weak cell, 80 ms capture. Folding PSS repeats at a
    # fixed 9600-sample spacing smears the peak (12 samples of drift); following the drift implied by the
    # CFO recovers it.
    fc = 806e6
    x = rtl_capture(309, 76, fc, 0.08, -3, seed=1)
    assert ls.cell_search(x, max_cfo_hz=70_000) == []
    cells = ls.cell_search(x, max_cfo_hz=70_000, fc_hz=fc)
    assert [c.pci for c in cells] == [309]
    assert abs(cells[0].cfo_hz - 76e-6 * fc) < 1_000
    assert ls.half_frame_period(61_256, fc) == pytest.approx(9600 * (1 - 61_256 / fc))


def test_ambiguity_echo_rejected_by_common_cfo():
    # Zadoff-Chu ambiguity: a strong cell also correlates with another PSS root ~120 samples earlier at a
    # ~19 kHz different CFO, and this echo passes the SSS thresholds. Real cells share the receiver's CFO.
    x = ls.synthesize([295], duration_s=0.03, snr_db=4.3, cfo_hz=10_053, frame_offset=11_056, seed=1018)
    cands = ls.candidates(x, max_cfo_hz=70_000, fc_hz=806e6)
    echoes = [c for c in cands if c.pci not in (None, 295) and c.sss_margin >= 1.6 and c.snr_db >= -12]
    assert echoes and all(abs(e.cfo_hz - 10_053) > 3_000 for e in echoes)
    assert [c.pci for c in ls.cell_search(x, max_cfo_hz=70_000, fc_hz=806e6)] == [295]
