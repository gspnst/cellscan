"""Tune an SDR across LTE channels and run the native PSS/SSS cell search on each."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from .. import spectrum
from ..models import Observation, Radio
from . import lte_sync
from .capture import Backend


def earfcns_for_band(band: int, step: int = 1) -> list[int]:
    b = spectrum.LTE_BANDS.get(band)
    if not b:
        raise ValueError(f"unknown LTE band {band}")
    return list(range(b.dl_offset, b.dl_high_channel + 1, step))


def candidate_earfcns(centers_mhz: Iterable[float], band: int | None = None, spread: int = 2) -> list[int]:
    """EARFCNs near carrier centres from a sweep (±spread raster steps, 100 kHz each)."""
    out: set[int] = set()
    for mhz in centers_mhz:
        raster = round(mhz * 10) / 10
        b = spectrum.LTE_BANDS.get(band) if band else next(
            (x for x in spectrum.LTE_BANDS.values() if x.dl_low <= raster <= x.dl_high), None)
        if not b:
            continue
        center = b.earfcn(raster)
        out.update(e for e in range(center - spread, center + spread + 1) if b.dl_offset <= e <= b.dl_high_channel)
    return sorted(out)


def scan(backend: Backend, earfcns: Iterable[int], duration_s: float = 0.03, gain: float | None = None,
         progress: Callable[[int, int, float], None] | None = None, device: str = "sdr") -> list[Observation]:
    earfcns = list(earfcns)
    n = int(duration_s * lte_sync.FS)
    found: dict[int, tuple[float, Observation]] = {}  # best observation per PCI
    for i, earfcn in enumerate(earfcns):
        b = spectrum.lte_band_for_earfcn(earfcn)
        if b is None:
            continue
        mhz = b.dl_mhz(earfcn)
        if progress:
            progress(i, len(earfcns), mhz)
        x = backend.capture(mhz * 1e6, lte_sync.FS, n, gain)
        for cell in lte_sync.cell_search(x):
            score = cell.pss_metric
            prev = found.get(cell.pci)
            # A cell is detectable a few raster steps off-centre; keep the strongest detection.
            if prev and prev[0] >= score:
                continue
            found[cell.pci] = (score, Observation(
                radio=Radio.LTE, source="sdr-lte", timestamp=datetime.now(timezone.utc), serving=False, device=device,
                channel=earfcn, band=b.number, pci=cell.pci, rel_power=round(cell.power_dbfs, 1), sinr=round(cell.snr_db, 1),
                notes=f"PSS {cell.pss_metric:.2f}, SSS margin {cell.sss_metric:.2f}, CFO {cell.cfo_hz / 1e3:+.1f} kHz"))
    return [o for _, o in sorted(found.values(), key=lambda t: -t[0])]
