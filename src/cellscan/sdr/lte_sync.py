"""LTE cell search from raw IQ: PSS/SSS detection per 3GPP TS 36.211 §6.11 (FDD, normal CP).

Works on 1.92 Msps complex baseband centred on the carrier (the six central resource blocks carry
PSS/SSS for every LTE bandwidth). Steps:

1. Correlate against the three time-domain PSS replicas over a grid of carrier-frequency offsets and
   accumulate non-coherently over 5 ms half-frames  ->  N_ID_2, symbol timing, coarse CFO.
2. For each PSS occurrence take the SSS symbol just before it, equalise with the PSS channel estimate
   and test all 168 N_ID_1 values for subframe 0/5  ->  N_ID_1 and frame timing.
3. PCI = 3 * N_ID_1 + N_ID_2.

Pure numpy, receive-only. Accuracy is limited by capture length and the SDR's frequency error.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import numpy as np

FS = 1.92e6
NFFT = 128
CP0, CP = 10, 9  # normal cyclic prefix, first / other symbols of a slot
HALF_FRAME = 9600  # 5 ms
FRAME = 19200
PSS_ROOTS = (25, 29, 34)
# Offset of the PSS useful part (after CP) from the start of subframe 0 / 5.
PSS_OFFSET_IN_SUBFRAME = (CP0 + NFFT) + 5 * (CP + NFFT) + CP  # 832


# --------------------------------------------------------------------------- sequences


@cache
def pss_freq(nid2: int) -> np.ndarray:
    u = PSS_ROOTS[nid2]
    n = np.arange(62)
    d = np.where(n < 31, np.exp(-1j * np.pi * u * n * (n + 1) / 63),
                 np.exp(-1j * np.pi * u * (n + 1) * (n + 2) / 63))
    return d.astype(np.complex64)


def _mseq(taps: tuple[int, ...]) -> np.ndarray:
    """31-length m-sequence with x(i+5) = sum(x(i+t) for t in taps) mod 2, x(0..4) = 0,0,0,0,1."""
    x = [0, 0, 0, 0, 1]
    for i in range(26):
        x.append(sum(x[i + t] for t in taps) % 2)
    return 1 - 2 * np.array(x[:31])


S_TILDE = _mseq((2, 0))
C_TILDE = _mseq((3, 0))
Z_TILDE = _mseq((4, 2, 1, 0))


def _m0_m1(nid1: int) -> tuple[int, int]:
    q_prime = nid1 // 30
    q = (nid1 + q_prime * (q_prime + 1) // 2) // 30
    m_prime = nid1 + q * (q + 1) // 2
    m0 = m_prime % 31
    m1 = (m0 + m_prime // 31 + 1) % 31
    return m0, m1


@cache
def sss_freq(nid1: int, nid2: int, subframe: int) -> np.ndarray:
    """62 SSS values for subframe 0 or 5."""
    m0, m1 = _m0_m1(nid1)
    n = np.arange(31)
    s0, s1 = S_TILDE[(n + m0) % 31], S_TILDE[(n + m1) % 31]
    c0, c1 = C_TILDE[(n + nid2) % 31], C_TILDE[(n + nid2 + 3) % 31]
    z1_m0, z1_m1 = Z_TILDE[(n + (m0 % 8)) % 31], Z_TILDE[(n + (m1 % 8)) % 31]
    d = np.empty(62)
    if subframe == 0:
        d[0::2], d[1::2] = s0 * c0, s1 * c1 * z1_m0
    else:
        d[0::2], d[1::2] = s1 * c0, s0 * c1 * z1_m1
    return d.astype(np.float32)


# Subcarriers k = -31..-1, +1..+31 (DC unused) mapped to FFT bins.
BINS = np.concatenate([np.arange(NFFT - 31, NFFT), np.arange(1, 32)])


def to_time(freq62: np.ndarray) -> np.ndarray:
    X = np.zeros(NFFT, dtype=np.complex64)
    X[BINS] = freq62
    return np.fft.ifft(X).astype(np.complex64) * np.sqrt(NFFT)


@cache
def pss_time(nid2: int) -> np.ndarray:
    return to_time(pss_freq(nid2))


def from_time(symbol128: np.ndarray) -> np.ndarray:
    return (np.fft.fft(symbol128) / np.sqrt(NFFT))[BINS]


# --------------------------------------------------------------------------- detection


@dataclass
class LTECell:
    pci: int
    nid1: int
    nid2: int
    frame_start: int  # sample index of a radio-frame start in the capture
    cfo_hz: float
    pss_metric: float  # normalised PSS correlation peak (0..1)
    sss_metric: float  # SSS decision margin (best / second best)
    power_dbfs: float  # mean PSS/SSS resource-element power relative to full scale
    snr_db: float  # estimate from SSS residual after equalisation


def _correlate(x: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """|cross-correlation| of x with ref, normalised by local energy (0..1)."""
    n = len(x) + len(ref) - 1
    size = 1 << (n - 1).bit_length()
    c = np.fft.ifft(np.fft.fft(x, size) * np.conj(np.fft.fft(ref, size)))[: len(x) - len(ref) + 1]
    energy = np.convolve(np.abs(x) ** 2, np.ones(len(ref)), mode="valid")
    return np.abs(c) / np.sqrt(np.maximum(energy, 1e-20) * np.sum(np.abs(ref) ** 2))


def _rotate(x: np.ndarray, cfo_hz: float, start: int = 0) -> np.ndarray:
    if cfo_hz == 0:
        return x
    n = np.arange(start, start + len(x))
    return (x * np.exp(-2j * np.pi * cfo_hz * n / FS)).astype(np.complex64)


def half_frame_period(cfo_hz: float, fc_hz: float | None) -> float:
    """Samples per 5 ms in the capture.

    SDRs derive the LO and the sample clock from the same crystal, so a frequency offset of `cfo_hz` at
    `fc_hz` implies a sample-rate error of -cfo/fc: at 76 ppm the PSS drifts 0.73 samples per half-frame,
    12 samples over 80 ms – enough to smear it away if the repeats are folded at a fixed 9600 spacing.
    """
    if not fc_hz:
        return float(HALF_FRAME)
    return HALF_FRAME * (1 - cfo_hz / fc_hz)


def occurrence_starts(timing: int, period: float, length: int) -> list[int]:
    """Sample index of every PSS repeat in a capture of `length` samples."""
    out, k = [], 0
    while True:
        t = timing + int(round(k * period))
        if t + NFFT > length:
            return out
        out.append(t)
        k += 1


def _fold(corr: np.ndarray, period: float) -> np.ndarray:
    """Average of the correlation over successive half-frames, following the clock drift."""
    folded = np.zeros(HALF_FRAME)
    k = 0
    while True:
        s = int(round(k * period))
        if s + HALF_FRAME > len(corr):
            break
        folded += corr[s : s + HALF_FRAME]
        k += 1
    if k == 0:  # shorter than one half-frame of correlation output
        folded[: len(corr)] = corr[:HALF_FRAME]
        return folded
    return folded / k


def detect_pss(x: np.ndarray, cfo_grid: np.ndarray, min_metric: float = 0.25,
               max_per_nid2: int = 3, fc_hz: float | None = None) -> list[tuple[int, int, float, float, bool]]:
    """Returns [(nid2, timing within half-frame, cfo_hz, metric, drift)] sorted by metric.

    Several peaks per N_ID_2 are kept (unsynchronised neighbour cells have different timing). With `fc_hz`
    the half-frame folding is done twice – at a fixed 9600-sample spacing and following the sample-clock
    drift implied by the CFO (shared LO/sample crystal) – and the stronger result is kept (`drift` flag).
    """
    periods = len(x) // HALF_FRAME
    if periods < 1:
        raise ValueError("capture too short: need at least 5 ms (9600 samples at 1.92 Msps)")
    results = []
    for nid2 in range(3):
        ref = pss_time(nid2)
        modes = (False, True) if fc_hz else (False,)
        folded_all = np.zeros((len(cfo_grid) * len(modes), HALF_FRAME))
        for i, cfo in enumerate(cfo_grid):
            corr = _correlate(_rotate(x, cfo), ref)
            for m, drift in enumerate(modes):
                folded_all[i * len(modes) + m] = _fold(corr, half_frame_period(float(cfo), fc_hz if drift else None))
        envelope = folded_all.max(axis=0)
        best_row = folded_all.argmax(axis=0)
        taken: list[int] = []
        for t in np.argsort(envelope)[::-1]:
            if envelope[t] < min_metric or len(taken) >= max_per_nid2:
                break
            if any(min(abs(int(t) - u), HALF_FRAME - abs(int(t) - u)) < 32 for u in taken):
                continue
            taken.append(int(t))
            row = int(best_row[t])
            results.append((nid2, int(t), float(cfo_grid[row // len(modes)]), float(envelope[t]), modes[row % len(modes)]))
    return sorted(results, key=lambda r: -r[3])


def _refine_cfo(x: np.ndarray, starts: list[int], nid2: int) -> float:
    """Fractional CFO from the phase drift between the two halves of each PSS."""
    ref = pss_time(nid2)
    acc = 0j
    for s in starts:
        seg = x[s : s + NFFT]
        if len(seg) < NFFT:
            continue
        a = np.vdot(ref[:64], seg[:64])
        b = np.vdot(ref[64:], seg[64:])
        acc += b * np.conj(a)
    if acc == 0:
        return 0.0
    return float(np.angle(acc) / (2 * np.pi * 64 / FS))


def detect_sss(x: np.ndarray, nid2: int, timing: int | list[int]) -> tuple[int, int, float, float, float, float] | None:
    """Returns (nid1, frame_start, margin, power_dbfs, snr_db, cfo_residual) for a CFO-corrected capture.

    `timing` is the PSS position within the first half-frame, or the explicit list of PSS positions
    (drift-corrected) in the capture.
    """
    positions = timing if isinstance(timing, list) else list(range(timing, len(x) - NFFT, HALF_FRAME))
    starts = [t for t in positions if t - NFFT - CP >= 0 and t + NFFT <= len(x)]
    if not starts:
        return None
    pss_ref = pss_freq(nid2)
    observations = []
    smooth = np.ones(7) / 7
    for t in starts:
        Y_pss = from_time(x[t : t + NFFT])
        # Channel estimate from the PSS, smoothed over neighbouring subcarriers to suppress noise and
        # interference from other cells; maximum-ratio combining instead of zero-forcing division.
        H_raw = Y_pss / pss_ref
        H = np.convolve(H_raw, smooth, mode="same") / np.convolve(np.ones(62), smooth, mode="same")
        Y_sss = from_time(x[t - NFFT - CP : t - CP])
        eq = Y_sss * np.conj(H)
        observations.append((eq, Y_pss, Y_sss, H))

    hyp = np.array([[sss_freq(n1, nid2, sf) for sf in (0, 5)] for n1 in range(168)])  # (168, 2, 62)
    metrics = np.array([np.real(hyp @ eq) for eq, *_ in observations])  # (occ, 168, 2)
    best_score, best = -np.inf, (0, 0)
    scores = np.zeros((168, 2))
    for phase in (0, 1):
        sel = np.array([(j + phase) % 2 for j in range(len(observations))])
        total = metrics[np.arange(len(observations)), :, sel].sum(axis=0)
        scores[:, phase] = total
        n1 = int(np.argmax(total))
        if total[n1] > best_score:
            best_score, best = total[n1], (n1, phase)
    nid1, phase = best
    flat = np.sort(scores.ravel())
    margin = float(flat[-1] / max(flat[-2], 1e-9)) if flat[-2] > 0 else float("inf")
    # Occurrence j carries subframe 0 when (j + phase) % 2 == 0.
    j0 = 0 if phase == 0 else 1
    frame_start = starts[j0] - PSS_OFFSET_IN_SUBFRAME if j0 < len(starts) else starts[0] - PSS_OFFSET_IN_SUBFRAME - HALF_FRAME
    frame_start %= FRAME

    signal_power, noise_power = [], []
    for j, (_eq, Y_pss, Y_sss, H) in enumerate(observations):
        sf = 0 if (j + phase) % 2 == 0 else 5
        ref = sss_freq(nid1, nid2, sf)
        signal_power.append(np.mean(np.abs(Y_pss) ** 2) + np.mean(np.abs(Y_sss) ** 2))
        noise_power.append(np.mean(np.abs(Y_sss - H * ref) ** 2))
    p_sig = float(np.mean(signal_power)) / 2
    p_noise = float(np.mean(noise_power))
    snr = 10 * np.log10(max(p_sig - p_noise, 1e-12) / max(p_noise, 1e-12))
    power_dbfs = 10 * np.log10(max(p_sig, 1e-20) / NFFT)
    return nid1, int(frame_start), margin, float(power_dbfs), float(snr), 0.0


@dataclass
class Candidate:
    """A PSS peak and the SSS decision for it, before acceptance thresholds (for diagnostics)."""

    nid2: int
    timing: int
    cfo_hz: float
    pss_metric: float
    nid1: int | None
    sss_margin: float
    snr_db: float
    power_dbfs: float
    frame_start: int | None

    @property
    def pci(self) -> int | None:
        return None if self.nid1 is None else 3 * self.nid1 + self.nid2


def candidates(x: np.ndarray, max_cfo_hz: float = 20_000, cfo_step_hz: float = 2_500,
               min_pss_metric: float = 0.25, fc_hz: float | None = None) -> list[Candidate]:
    """All PSS peaks above `min_pss_metric` with their best SSS hypothesis, strongest PSS first.

    Pass the tuned frequency `fc_hz` to compensate the sample-clock drift of SDRs with large crystal
    errors (essential for long captures from RTL-SDR dongles without TCXO).
    """
    x = np.asarray(x, dtype=np.complex64)
    x = x - np.mean(x)  # remove DC (common on zero-IF SDRs)
    grid = np.arange(-max_cfo_hz, max_cfo_hz + 1, cfo_step_hz)
    out: list[Candidate] = []
    for nid2, timing, cfo, metric, drift in detect_pss(x, grid, min_pss_metric, fc_hz=fc_hz):
        fc = fc_hz if drift else None
        coarse = _rotate(x, cfo)
        starts = occurrence_starts(timing, half_frame_period(cfo, fc), len(x))
        fine = cfo + _refine_cfo(coarse, starts, nid2)
        starts = occurrence_starts(timing, half_frame_period(fine, fc), len(x))
        res = detect_sss(_rotate(x, fine), nid2, starts)
        if res is None:
            out.append(Candidate(nid2, timing, fine, metric, None, 0.0, float("-inf"), float("-inf"), None))
            continue
        nid1, frame_start, margin, power, snr, _ = res
        out.append(Candidate(nid2, timing, fine, metric, nid1, margin, snr, power, frame_start))
    return out


def capture_stats(x: np.ndarray) -> dict[str, float]:
    """Level, clipping and DC of a capture – the first things to check when nothing is decoded."""
    x = np.asarray(x, dtype=np.complex64)
    if len(x) == 0:
        return {"samples": 0, "rms_dbfs": float("-inf"), "clip_pct": 0.0, "dc_dbfs": float("-inf")}
    rms = float(np.sqrt(np.mean(np.abs(x) ** 2)))
    clip = float(np.mean((np.abs(x.real) >= 0.99) | (np.abs(x.imag) >= 0.99)) * 100)
    dc = float(np.abs(np.mean(x)))
    return {"samples": len(x), "rms_dbfs": 20 * np.log10(max(rms, 1e-9)), "clip_pct": clip,
            "dc_dbfs": 20 * np.log10(max(dc, 1e-9))}


def cell_search(x: np.ndarray, max_cfo_hz: float = 20_000, cfo_step_hz: float = 2_500,
                min_pss_metric: float = 0.25, min_sss_margin: float = 1.6, min_snr_db: float = -12.0,
                fc_hz: float | None = None, max_cfo_spread_hz: float = 3_000) -> list[LTECell]:
    """Finds LTE cells in a 1.92 Msps capture (ideally >= 20 ms). Strongest first.

    A PSS peak is only accepted when the SSS that precedes it matches one hypothesis clearly
    (margin) and fits well after equalisation (SNR); this rejects PSS sidelobes and the
    timing/frequency ambiguity of Zadoff-Chu sequences.
    """
    cells: list[LTECell] = []
    accepted = [c for c in candidates(x, max_cfo_hz, cfo_step_hz, min_pss_metric, fc_hz=fc_hz)
                if c.nid1 is not None and c.sss_margin >= min_sss_margin and c.snr_db >= min_snr_db]
    accepted.sort(key=lambda c: -c.pss_metric)
    for c in accepted:
        # All real cells in one capture share the receiver's frequency offset (base stations are accurate to
        # 0.05 ppm), so a candidate offset by more than a few kHz from the strongest cell is a Zadoff-Chu
        # timing/frequency ambiguity echo of it.
        if cells and abs(c.cfo_hz - cells[0].cfo_hz) > max_cfo_spread_hz:
            continue
        if any(cell.pci == c.pci for cell in cells):
            continue
        cells.append(LTECell(c.pci, c.nid1, c.nid2, c.frame_start, c.cfo_hz, c.pss_metric, c.sss_margin,  # type: ignore[arg-type]
                             c.power_dbfs, c.snr_db))
    return sorted(cells, key=lambda c: -c.power_dbfs)


# --------------------------------------------------------------------------- synthesis (tests & demo)


def synthesize(pcis: list[int], duration_s: float = 0.02, gains_db: list[float] | None = None, snr_db: float = 10.0,
               cfo_hz: float = 0.0, frame_offset: int = 0, seed: int = 0) -> np.ndarray:
    """1.92 Msps baseband with PSS/SSS (and random QPSK filler on the central 72 subcarriers) for each PCI."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * FS)
    total = np.zeros(n + FRAME, dtype=np.complex64)
    gains_db = gains_db or [0.0] * len(pcis)
    filler_bins = np.concatenate([np.arange(NFFT - 36, NFFT), np.arange(1, 37)])
    for pci, g_db in zip(pcis, gains_db):
        nid1, nid2 = divmod(pci, 3)
        sig = np.zeros(n + FRAME, dtype=np.complex64)
        pos = 0
        sym_in_frame = 0
        while pos < len(sig) - (CP0 + NFFT):
            slot_sym = sym_in_frame % 7
            slot = (sym_in_frame // 7) % 20
            cp = CP0 if slot_sym == 0 else CP
            X = np.zeros(NFFT, dtype=np.complex64)
            qpsk = (rng.choice([-1, 1], 72) + 1j * rng.choice([-1, 1], 72)) / np.sqrt(2)
            X[filler_bins] = qpsk * 0.7
            if slot in (0, 10) and slot_sym == 6:
                X[filler_bins] = 0
                X[BINS] = pss_freq(nid2)
            elif slot in (0, 10) and slot_sym == 5:
                X[filler_bins] = 0
                X[BINS] = sss_freq(nid1, nid2, 0 if slot == 0 else 5)
            sym = np.fft.ifft(X) * np.sqrt(NFFT)
            block = np.concatenate([sym[-cp:], sym])
            end = min(pos + len(block), len(sig))
            sig[pos:end] = block[: end - pos]
            pos += len(block)
            sym_in_frame += 1
        total += sig * 10 ** (g_db / 20)
    total = total[(FRAME - frame_offset) % FRAME :][:n]
    p = np.mean(np.abs(total) ** 2)
    noise = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * np.sqrt(p / 2 / 10 ** (snr_db / 10))
    out = total + noise
    if cfo_hz:
        out = out * np.exp(2j * np.pi * cfo_hz * np.arange(n) / FS)
    return (out * 0.1).astype(np.complex64)
