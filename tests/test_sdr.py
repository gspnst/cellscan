"""SDR plumbing tests. Tool outputs below are modelled on the documented/printed formats of each tool."""

import numpy as np
import pytest

from cellscan.models import Family, Radio
from cellscan.sdr import capture, external, lte_scan, lte_sync, sweep


def test_sample_formats():
    assert np.allclose(capture.from_cu8(bytes([255, 0])), [1 - 1j], atol=0.01)
    assert np.allclose(capture.from_cs8(np.array([64, -64], dtype=np.int8).tobytes()), [0.5 - 0.5j])
    z = np.array([1 + 2j, -3j], dtype=np.complex64)
    assert np.allclose(capture.from_cf32(z.tobytes()), z)


def test_resample_preserves_tone():
    fs_in, fs_out, f = 2.4e6, 1.92e6, 100e3
    n = np.arange(48000)
    x = np.exp(2j * np.pi * f * n / fs_in).astype(np.complex64)
    y = capture.to_rate(x, fs_in, fs_out)
    assert abs(len(y) - len(x) * 0.8) <= 2
    spec = np.abs(np.fft.fft(y[1000:9192]))
    peak = np.fft.fftfreq(8192, 1 / fs_out)[np.argmax(spec)]
    assert abs(peak - f) < 500


def test_file_backend_roundtrip(tmp_path):
    x = lte_sync.synthesize([301], duration_s=0.03, snr_db=10, seed=1)
    path = tmp_path / "rec.cf32"
    path.write_bytes(x.tobytes())
    backend = capture.open_backend(str(path))
    y = backend.capture(806e6, lte_sync.FS, len(x), None)
    assert np.allclose(x, y)
    assert lte_sync.cell_search(y)[0].pci == 301
    with pytest.raises(capture.CaptureError):
        capture.open_backend("nonexistent-sdr")


RTL_POWER = "2026-01-01, 12:00:00, 790000000, 800000000, 100000.00, 12, " + ", ".join(["-30.0"] * 100) + "\n"


def synthetic_spectrum():
    freqs = np.arange(791e6, 862e6, 50e3) + 25e3
    rng = np.random.default_rng(0)
    p = -60 + rng.normal(0, 0.7, len(freqs))
    # 10 MHz LTE carrier centred on 806 MHz (EARFCN 6300) with a DC notch
    m = np.abs(freqs - 806e6) < 4.5e6
    p[m] = -35 + rng.normal(0, 0.7, m.sum())
    p[np.abs(freqs - 806e6) < 60e3] = -50
    # 200 kHz GSM-like carrier at 947.4 MHz is outside; add a narrow one at 820.0 MHz
    p[np.abs(freqs - 820e6) < 100e3] = -40
    return sweep.Spectrum(freqs, p)


def test_parse_power_csv():
    s = sweep.parse_power_csv(RTL_POWER)
    assert len(s.freqs_hz) == 100 and s.bin_hz == pytest.approx(100_000)
    assert s.freqs_hz[0] == pytest.approx(790.05e6)
    with pytest.raises(sweep.SweepError):
        sweep.parse_power_csv("garbage")


def test_detect_carriers():
    carriers = sweep.detect_carriers(synthetic_spectrum(), mcc=230)
    lte = next(c for c in carriers if c.kind != "GSM")
    assert lte.family is Family.LTE and lte.band_label == "B20" and abs(lte.channel - 6300) <= 1
    assert lte.width_mhz == 10
    narrow = next(c for c in carriers if c.width_mhz < 0.45)
    assert abs(narrow.center_mhz - 820.0) < 0.1 and narrow.kind == "narrowband"  # 820 MHz is no GSM downlink


def test_classify_gsm_and_nr():
    g = sweep.classify(947.4, 0.2, -40, 20)
    assert (g.kind, g.family, g.channel) == ("GSM", Family.GSM, 62)
    nr = sweep.classify(3550.0, 90, -50, 15, mcc=230)
    assert nr.family is Family.NR and nr.band_label == "n78"
    assert sweep.band_range_hz("B20") == (791e6, 821e6)
    assert sweep.band_range_hz("n78")[0] == 3300e6


KAL = """kal: Scanning for GSM-900 base stations.
GSM-900:
	chan: 1 (935.2MHz - 13.614kHz)	power: 1105304.55
	chan: 11 (937.2MHz + 2.483kHz)	power:  640012.85
"""

GRGSM = """ARFCN:   62, Freq:  947.4M, CID: 12345, LAC:  4321, MCC: 230, MNC:   1, Pwr: -36
|---- Configuration: 1 CCCH, not combined
|---- Cell ARFCNs: 62, 70
|---- Neighbour Cells: 3, 15, 28
ARFCN:   70, Freq:  949.0M, CID: 22222, LAC:  4321, MCC:   1, MNC:   1, Pwr: -20
"""

SRSRAN = """[ 10/ 75]: EARFCN 6300 Freq. 806.00 MHz looking for PSS.
Found CELL 806.0 MHz, EARFCN=6300, PHYID=120, 50 PRB, 2 ports, PSS power=-28.5 dBm

Found 2 cells
  EARFCN=6300 f=806.0 MHz, PCI=120, PRB=50, Ports=2, CFO=-1.2 KHz
  EARFCN=6400 f=816.0 MHz, PCI=77, PRB=50, Ports=2, CFO=0.4 KHz
"""

LCS = """Detected the following cells:
A: #antenna ports C: CP type ; P: PHICH duration ; PR: PHICH resource type
CID A      fc   foff RXPWR C nRB P  PR CrystalCorrectionFactor
120 2   806M  -2.1k  -28.4 N  50 N 1/6 0.99999740
"""


def test_external_parsers():
    kal = external.parse_kal(KAL)
    assert [(c.arfcn, c.offset_khz) for c in kal] == [(1, -13.614), (11, 2.483)]
    assert external.kal_observations(kal)[0].radio is Radio.GSM
    gsm = external.parse_grgsm(GRGSM)
    assert [(o.channel, o.cell_id, o.area_code, o.mcc, o.mnc, o.rel_power) for o in gsm] == [
        (62, 12345, 4321, 230, 1, -36.0), (70, 22222, 4321, 1, 1, -20.0)]
    assert gsm[0].notes == "neighbours: 3, 15, 28"
    srs = external.parse_srsran(SRSRAN)
    assert {(o.channel, o.pci) for o in srs} == {(6300, 120), (6400, 77)}
    first = next(o for o in srs if o.pci == 120)
    assert first.bandwidth_mhz == 10.0 and first.rel_power == -28.5 and "CFO -1.2" in first.notes
    lcs = external.parse_lte_cell_scanner(LCS)
    assert (lcs[0].pci, lcs[0].channel, lcs[0].bandwidth_mhz) == (120, 6300, 10.0)


class FakeSDR:
    name = "fake"

    def __init__(self, cells_by_earfcn):
        self.cells = cells_by_earfcn
        self.tuned = []

    def capture(self, freq_hz, rate, n, gain):
        from cellscan import spectrum

        earfcn = spectrum.LTE_BANDS[20].earfcn(freq_hz / 1e6)
        self.tuned.append(earfcn)
        pcis = self.cells.get(earfcn)
        if not pcis:
            rng = np.random.default_rng(earfcn)
            return (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.01
        return lte_sync.synthesize(pcis, duration_s=n / lte_sync.FS, snr_db=8, seed=earfcn)


def test_lte_scan_with_fake_sdr():
    sdr = FakeSDR({6300: [120], 6400: [77]})
    progress = []
    obs = lte_scan.scan(sdr, [6299, 6300, 6301, 6400], progress=lambda i, n, f: progress.append((i, n, f)))
    assert {(o.channel, o.pci) for o in obs} == {(6300, 120), (6400, 77)}
    assert all(o.source == "sdr-lte" and not o.serving and o.band == 20 for o in obs)
    assert progress[0] == (0, 4, 805.9)
    assert lte_scan.candidate_earfcns([806.03], band=20, spread=1) == [6299, 6300, 6301]
    assert len(lte_scan.earfcns_for_band(20)) == 300


def test_detect_carriers_no_edge_artifacts():
    freqs = np.arange(791e6, 821e6, 100e3) + 50e3
    rng = np.random.default_rng(1)
    p = -70 + rng.normal(0, 0.8, len(freqs))
    p[np.abs(freqs - 806e6) < 4.5e6] += 25
    carriers = sweep.detect_carriers(sweep.Spectrum(freqs, p))
    assert len(carriers) == 1 and carriers[0].channel == 6300


def test_lte_scan_wide_frequency_offset():
    """Cheap RTL-SDRs can be ~40 ppm off (35 kHz at 806 MHz): only the widened search finds the cell."""

    class OffsetSDR(FakeSDR):
        def capture(self, freq_hz, rate, n, gain):
            return lte_sync.synthesize([301], duration_s=n / lte_sync.FS, snr_db=8, cfo_hz=35_000, seed=9)

    assert lte_scan.scan(OffsetSDR({}), [6300]) == []
    found = lte_scan.scan(OffsetSDR({}), [6300], max_cfo_hz=50_000)
    assert [o.pci for o in found] == [301] and "CFO +35" in found[0].notes


def test_guided_lte_scan_cli(monkeypatch, capsys):
    from cellscan import cli
    from cellscan.sdr import capture

    freqs = np.arange(791e6, 821e6, 100e3) + 50e3
    p = -70 + np.random.default_rng(2).normal(0, 0.5, len(freqs))
    p[np.abs(freqs - 806e6) < 4.5e6] += 25
    sdr = FakeSDR({6300: [120]})
    monkeypatch.setattr(sweep, "run_sweep", lambda *a, **k: sweep.Spectrum(freqs, p))
    monkeypatch.setattr(capture, "open_backend", lambda *a, **k: sdr)
    assert cli.main(["lte-scan", "--sdr", "rtlsdr", "--band", "B20", "--guided"]) == 0
    assert sorted(sdr.tuned) == list(range(6297, 6304))  # only around the carrier found by the sweep (±3)
    out = capsys.readouterr().out
    assert "806.0 MHz" in out and "120" in out


def rtl_power_like(start, stop, carriers, seed=0, hop=2.0e6):
    """Sweep resembling rtl_power output: carriers with load ripple, hop-edge roll-off and DC spikes."""
    rng = np.random.default_rng(seed)
    freqs = np.arange(start, stop, 100e3) + 50e3
    p = -72.0 + rng.normal(0, 0.8, len(freqs))
    for center, level in carriers:
        occ = np.abs(freqs - center) < 4.5e6
        p[occ] = np.maximum(p[occ], level + rng.normal(0, 1.8, occ.sum()))
    pos = (freqs - start) % hop
    p -= np.where((pos < 0.15e6) | (pos > hop - 0.15e6), 3.0, 0.0)
    p += np.where(np.abs(pos - hop / 2) < 0.06e6, 4.0, 0.0)
    return sweep.Spectrum(freqs, p)


B20_FULL = [(796e6, -42), (806e6, -47), (816e6, -53)]  # three operators filling 791-821 MHz


def test_fully_occupied_band_needs_margin():
    lo, hi = sweep.band_sweep_range("B20")
    assert lo < 791e6 and hi > 821e6
    found = sweep.detect_carriers(rtl_power_like(lo, hi, B20_FULL), mcc=230, within_hz=sweep.band_range_hz("B20"))
    assert sorted(c.channel for c in found) == [6200, 6300, 6400]
    assert all(c.width_mhz == 10 and not c.kind.endswith("?") for c in found)


def test_gapless_block_is_tiled_with_all_layouts():
    lo, hi = sweep.band_sweep_range("B20")
    freqs = np.arange(lo, hi, 100e3) + 50e3
    p = -72 + np.random.default_rng(1).normal(0, 0.8, len(freqs))
    p[(freqs > 791e6) & (freqs < 821e6)] = -45
    found = sweep.detect_carriers(sweep.Spectrum(freqs, p), within_hz=sweep.band_range_hz("B20"))
    candidates = set(lte_scan.candidate_earfcns([c.center_mhz for c in found], band=20, spread=1))
    assert {6200, 6300, 6400} <= candidates  # 3 x 10 MHz layout covered
    assert all(c.kind.endswith("?") for c in found)


def test_guided_scan_on_full_band(monkeypatch, capsys):
    from cellscan import cli
    from cellscan.sdr import capture

    swept = {}

    def fake_run_sweep(tool, lo, hi, bin_hz, gain):
        swept["range"] = (lo, hi)
        return rtl_power_like(lo, hi, B20_FULL, seed=4)

    sdr = FakeSDR({6200: [33], 6300: [120], 6400: [401]})
    monkeypatch.setattr(sweep, "run_sweep", fake_run_sweep)
    monkeypatch.setattr(capture, "open_backend", lambda *a, **k: sdr)
    assert cli.main(["lte-scan", "--sdr", "rtlsdr", "--band", "B20", "--guided"]) == 0
    assert swept["range"][0] < 791e6 and swept["range"][1] > 821e6
    assert sorted(sdr.tuned) == [e for c in (6200, 6300, 6400) for e in range(c - 3, c + 4)]
    out = capsys.readouterr().out
    assert all(str(p) in out for p in (33, 120, 401))


def test_guided_scan_reports_flat_spectrum(monkeypatch, capsys):
    from cellscan import cli

    monkeypatch.setattr(sweep, "run_sweep", lambda tool, lo, hi, b, g: rtl_power_like(lo, hi, [], seed=5))
    assert cli.main(["lte-scan", "--sdr", "rtlsdr", "--band", "B20", "--guided"]) == 1
    captured = capsys.readouterr()
    assert "flat spectrum" in captured.out and "no LTE carriers" in captured.err


def rtl_power_csv(start, stop, signal, hop=2.647e6, bins=32, dc_spike_db=9.0, seed=0):
    """CSV text the way rtl_power prints it: one line per hop, DC spike in each hop's centre bins."""
    rng = np.random.default_rng(seed)
    lines, f0 = [], start
    while f0 < stop:
        step = hop / bins
        freqs = f0 + (np.arange(bins) + 0.5) * step
        values = -70 + rng.normal(0, 0.7, bins) + np.array([signal(f) for f in freqs])
        values[bins // 2 - 1 : bins // 2 + 1] += dc_spike_db
        lines.append(f"2026-11-14, 18:50:00, {int(f0)}, {int(f0 + hop)}, {step:.2f}, 40, " + ", ".join(f"{v:.2f}" for v in values))
        f0 += hop
    return "\n".join(lines) + "\n"


def test_rtl_power_dc_spikes_are_not_carriers():
    # Real-world case: B20 sweep where only the central 1.4 MHz of a lightly loaded carrier at 806.0 MHz
    # stands out, and rtl_power's DC spike sits near it.
    lo, hi = sweep.band_sweep_range("B20")
    text = rtl_power_csv(lo, hi, lambda f: 20.0 if abs(f - 806.0e6) < 0.6e6 else 0.0)
    spec = sweep.parse_power_csv(text)
    carriers = sweep.detect_carriers(spec, mcc=230)
    assert len(carriers) == 1, [c.describe() for c in carriers]
    c = carriers[0]
    assert c.family is Family.LTE and abs(c.center_mhz - 806.0) < 0.15
    assert 6300 in lte_scan.candidate_earfcns([c.center_mhz], band=20, spread=3)


def test_dc_spike_removal_keeps_wide_signals():
    values = [-60.0] * 32
    values[15] = values[16] = -45.0  # spike
    cleaned = sweep._remove_dc_spike(values)
    assert max(cleaned) == -60.0
    flat = [-40.0] * 32  # inside a carrier: unchanged level
    assert sweep._remove_dc_spike(flat) == flat
