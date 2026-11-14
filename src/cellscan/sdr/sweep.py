"""Wideband power sweeps (rtl_power / hackrf_sweep / soapy_power) and carrier detection."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

import numpy as np

from .. import spectrum
from ..models import Family


class SweepError(RuntimeError):
    pass


@dataclass
class Spectrum:
    freqs_hz: np.ndarray
    power_db: np.ndarray

    @property
    def bin_hz(self) -> float:
        return float(np.median(np.diff(self.freqs_hz))) if len(self.freqs_hz) > 1 else 0.0


@dataclass
class Carrier:
    center_mhz: float
    width_mhz: float
    peak_db: float
    snr_db: float
    kind: str  # "LTE/NR", "UMTS/LTE", "GSM", "narrowband", "wideband"
    family: Family | None = None
    band_label: str | None = None
    channel: int | None = None  # EARFCN / ARFCN / NR-ARFCN guess

    def describe(self) -> str:
        loc = f" {self.band_label} ch {self.channel}" if self.band_label and self.channel is not None else ""
        return f"{self.center_mhz:9.3f} MHz  {self.width_mhz:5.2f} MHz  {self.kind:<10}{loc}  +{self.snr_db:.0f} dB"


def parse_power_csv(text: str) -> Spectrum:
    """Parses rtl_power / hackrf_sweep / soapy_power CSV: date, time, f_low, f_high, step, samples, dB..."""
    rows: dict[float, float] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            f_low, f_high, step = float(parts[2]), float(parts[3]), float(parts[4])
            values = [float(v) for v in parts[6:] if v not in ("", "nan", "-inf", "inf")]
        except ValueError:
            continue
        if step <= 0 or not values:
            continue
        values = _remove_dc_spike(values)
        for i, v in enumerate(values):
            f = f_low + (i + 0.5) * step
            if f <= f_high + step:
                rows[round(f, 1)] = max(v, rows.get(round(f, 1), -1e9))
    if not rows:
        raise SweepError("no sweep data parsed")
    freqs = np.array(sorted(rows))
    return Spectrum(freqs, np.array([rows[f] for f in freqs]))


def _remove_dc_spike(values: list[float]) -> list[float]:
    """Each rtl_power hop shows the dongle's DC offset as a spike in its centre bin(s), which would look like
    narrow carriers every hop (e.g. every 2.6 MHz). Replace the centre bin(s) with the surrounding level."""
    n = len(values)
    if n < 8:
        return values
    centre = [n // 2] if n % 2 else [n // 2 - 1, n // 2]
    lo, hi = centre[0] - 2, centre[-1] + 2
    level = (values[lo] + values[hi]) / 2
    out = list(values)
    for i in range(centre[0] - 1, centre[-1] + 2):  # centre bin(s) plus one leakage bin on each side
        out[i] = level
    return out


def run_sweep(tool: str, start_hz: float, stop_hz: float, bin_hz: float = 100_000, gain: float | None = None,
              device: str | None = None, timeout: float = 120) -> Spectrum:
    tool = tool.lower()
    if tool in ("rtl", "rtlsdr", "rtl_power"):
        exe = "rtl_power"
        cmd = [exe, "-f", f"{int(start_hz)}:{int(stop_hz)}:{int(bin_hz)}", "-i", "1", "-1"]
        if gain is not None:
            cmd += ["-g", str(gain)]
        if device:
            cmd += ["-d", device]
        cmd.append("-")
    elif tool in ("hackrf", "hackrf_sweep"):
        exe = "hackrf_sweep"
        cmd = [exe, "-f", f"{int(start_hz // 1e6)}:{int(np.ceil(stop_hz / 1e6))}", "-w", str(int(bin_hz)), "-1",
               "-l", "32", "-g", str(int(gain if gain is not None else 30))]
        if device:
            cmd += ["-d", device]
    elif tool in ("soapy", "soapy_power"):
        exe = "soapy_power"
        cmd = [exe, "-f", f"{int(start_hz)}:{int(stop_hz)}", "-B", str(int(bin_hz)), "-O", "-", "-F", "rtl_power", "-n", "1"]
        if device:
            cmd += ["-d", device]
        if gain is not None:
            cmd += ["-g", str(gain)]
    else:
        raise SweepError(f"unknown sweep tool {tool}")
    if shutil.which(exe) is None:
        raise SweepError(f"{exe} not found (see `cellscan doctor`)")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0 and not proc.stdout:
        raise SweepError(f"{exe} failed: {proc.stderr.strip()[-300:]}")
    return parse_power_csv(proc.stdout)


_LTE_WIDTHS = (1.4, 3, 5, 10, 15, 20)


def _smooth(p: np.ndarray, k: int) -> np.ndarray:
    """Moving average without zero-padding artefacts at the edges."""
    return np.convolve(np.pad(p, k // 2, mode="edge"), np.ones(k) / k, mode="valid")[: len(p)]


def detect_carriers(spec: Spectrum, threshold_db: float = 8.0, mcc: int | None = None,
                    within_hz: tuple[float, float] | None = None) -> list[Carrier]:
    """Finds occupied channels: segments above (noise floor + threshold), classified by width.

    The noise floor is the 10th percentile of the sweep, so the sweep should include some unused
    spectrum (use `band_sweep_range`, which adds a margin around the band). Adjacent carriers that merge
    into one block wider than 21 MHz are split at power dips, or tiled with standard LTE widths.
    `within_hz` keeps only carriers whose centre lies in that range.
    """
    p = spec.power_db
    if len(p) < 5:
        return []
    smooth = _smooth(p, 3)
    floor = float(np.percentile(smooth, 10))
    above = smooth > floor + threshold_db
    bin_mhz = spec.bin_hz / 1e6
    max_gap = max(1, int(0.3 / bin_mhz)) if bin_mhz > 0 else 1  # bridge notches < 300 kHz (e.g. DC, hop edges)
    mask = above.copy()
    on = np.flatnonzero(mask)
    for a, b in zip(on, on[1:]):
        if 1 < b - a <= max_gap + 1:
            mask[a:b] = True
    edges = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    segments = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1) - 1))

    carriers = []
    for a, b in segments:
        for lo_hz, hi_hz, tiled in _split_wide(spec, smooth, int(a), int(b)):
            width = (hi_hz - lo_hz) / 1e6
            center = (lo_hz + hi_hz) / 2e6
            sel = (spec.freqs_hz >= lo_hz) & (spec.freqs_hz <= hi_hz)
            peak = float(p[sel].max()) if sel.any() else float(p[a : b + 1].max())
            if peak - floor < threshold_db:
                continue
            if within_hz and not within_hz[0] <= center * 1e6 <= within_hz[1]:
                continue
            c = classify(center, width, peak, peak - floor, mcc)
            if tiled:
                c.kind += "?"  # guessed from a gap-less block: one of several possible carrier layouts
            carriers.append(c)
    return sorted(carriers, key=lambda c: (c.kind.endswith("?"), -c.peak_db))


def _split_wide(spec: Spectrum, smooth: np.ndarray, a: int, b: int) -> list[tuple[float, float, bool]]:
    """Splits a segment wider than one LTE carrier at internal dips.

    A block without visible gaps is ambiguous (30 MHz may be 3×10 or 2×15 MHz), so it is tiled with every
    standard LTE width that fits; the guided LTE scan then tries all of those centres.
    """
    half = spec.bin_hz / 2
    lo, hi = spec.freqs_hz[a] - half, spec.freqs_hz[b] + half
    if (hi - lo) / 1e6 <= 21 or b - a < 10:
        return [(lo, hi, False)]
    seg = _smooth(smooth[a : b + 1], 5)
    dip = seg < np.median(seg) - 6
    dip[:3] = dip[-3:] = False
    pieces, start, i = [], 0, 0
    while i < len(dip):
        if dip[i]:
            j = i
            while j + 1 < len(dip) and dip[j + 1]:
                j += 1
            if j - i + 1 >= 3:  # >= 300 kHz guard gap between carriers
                pieces.append((start, i - 1))
                start = j + 1
            i = j + 1
        else:
            i += 1
    pieces.append((start, len(dip) - 1))
    out: list[tuple[float, float, bool]] = []
    for s_i, e_i in pieces:
        p_lo, p_hi = spec.freqs_hz[a + s_i] - half, spec.freqs_hz[a + e_i] + half
        width = (p_hi - p_lo) / 1e6
        if width <= 21:
            out.append((p_lo, p_hi, False))
            continue
        widths = [w for w in (20, 15, 10, 5) if abs(width / w - round(width / w)) < 0.15] or [10]
        seen: list[float] = []
        for w in widths:
            n = max(1, round(width / w))
            tile = (p_hi - p_lo) / n
            for k in range(n):
                t_lo, t_hi = p_lo + k * tile, p_lo + (k + 1) * tile
                centre = (t_lo + t_hi) / 2
                if all(abs(centre - c) > 150e3 for c in seen):
                    seen.append(centre)
                    out.append((t_lo, t_hi, True))
    return out


def classify(center_mhz: float, width_mhz: float, peak_db: float, snr_db: float, mcc: int | None = None) -> Carrier:
    c = Carrier(round(center_mhz, 3), round(width_mhz, 2), peak_db, snr_db, "narrowband")
    if width_mhz < 0.45:
        arfcn = spectrum.gsm_arfcn_for_dl(round(center_mhz * 5) / 5, mcc)
        if arfcn is not None:
            info = spectrum.gsm_channel_info(arfcn, mcc)
            c.kind = "GSM"
            c.family, c.channel, c.band_label = Family.GSM, arfcn, info.band_name if info else None
        return c
    if width_mhz > 105:
        c.kind = "wideband"
        return c
    nearest = min(_LTE_WIDTHS, key=lambda w: abs(w * 0.9 - width_mhz))
    c.kind = "UMTS/LTE" if 3.8 <= width_mhz <= 5.2 else ("LTE/NR" if width_mhz <= 21 else "NR")
    raster = round(center_mhz * 10) / 10
    band = next((b for b in spectrum.LTE_BANDS.values() if b.dl_low <= raster <= b.dl_high and b.duplex != "TDD"), None) \
        or next((b for b in spectrum.LTE_BANDS.values() if b.dl_low <= raster <= b.dl_high), None)
    if band and width_mhz <= 21:
        c.family, c.channel, c.band_label = Family.LTE, band.earfcn(raster), f"B{band.number}"
        c.width_mhz = nearest if abs(nearest * 0.9 - width_mhz) < 1.5 else c.width_mhz
    else:
        arfcn = spectrum.mhz_to_nr_arfcn(center_mhz)
        nr = spectrum.nr_likely_band(arfcn, mcc) if arfcn is not None else None
        if nr:
            c.family, c.channel, c.band_label = Family.NR, arfcn, f"n{nr.number}"
    return c


def band_sweep_range(label: str, margin_mhz: float | None = None) -> tuple[float, float]:
    """Band downlink range plus a margin of unused spectrum on both sides for the noise-floor estimate."""
    lo, hi = band_range_hz(label)
    margin = (margin_mhz if margin_mhz is not None else max(5.0, min(15.0, 0.25 * (hi - lo) / 1e6))) * 1e6
    return max(lo - margin, 24e6), hi + margin


def band_range_hz(label: str) -> tuple[float, float]:
    """'B20' / 'n78' / '20' / 'gsm900' / 'gsm1800' -> downlink range in Hz."""
    lab = label.lower()
    if lab in ("gsm900", "egsm"):
        return 925e6, 960e6
    if lab in ("gsm1800", "dcs"):
        return 1805e6, 1880e6
    if lab in ("gsm850",):
        return 869e6, 894e6
    if lab in ("pcs", "gsm1900"):
        return 1930e6, 1990e6
    if lab.startswith("n"):
        b = spectrum.NR_BANDS.get(int(lab[1:]))
        if b:
            return b.dl_low * 1e6, b.dl_high * 1e6
    num = int(lab.lstrip("b"))
    b = spectrum.LTE_BANDS.get(num)
    if not b:
        raise SweepError(f"unknown band {label}")
    return b.dl_low * 1e6, b.dl_high * 1e6
