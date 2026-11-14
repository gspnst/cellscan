"""Wrappers for established receive-only scanners, parsed into observations.

* kalibrate-rtl `kal -s GSM900`        – GSM carriers + frequency offset (also good for SDR ppm calibration)
* gr-gsm `grgsm_scanner`               – GSM cells with broadcast identity (MCC/MNC/LAC/CID) from BCCH
* srsRAN 4G `cell_search`              – LTE cells incl. MIB (PRB, antenna ports)
* LTE-Cell-Scanner `CellSearch`        – LTE cells incl. frequency offset
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models import Observation, Radio


class ToolError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: float) -> str:
    if shutil.which(cmd[0]) is None:
        raise ToolError(f"{cmd[0]} not found (see `cellscan doctor`)")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        if out:
            return out
        raise ToolError(f"{cmd[0]} timed out") from exc
    return proc.stdout + "\n" + proc.stderr


# --------------------------------------------------------------------------- kalibrate


@dataclass
class KalChannel:
    arfcn: int
    freq_mhz: float
    offset_khz: float
    power: float


_KAL = re.compile(r"chan:\s*(\d+)\s*\(([\d.]+)MHz\s*([+-])\s*([\d.]+)kHz\)\s*power:\s*([\d.]+)")


def parse_kal(text: str) -> list[KalChannel]:
    out = []
    for m in _KAL.finditer(text):
        sign = -1 if m.group(3) == "-" else 1
        out.append(KalChannel(int(m.group(1)), float(m.group(2)), sign * float(m.group(4)), float(m.group(5))))
    return out


def kal_scan(band: str = "GSM900", gain: float | None = None, timeout: float = 180) -> list[KalChannel]:
    cmd = ["kal", "-s", band]
    if gain is not None:
        cmd += ["-g", str(gain)]
    return parse_kal(_run(cmd, timeout))


def kal_observations(channels: list[KalChannel], device: str = "sdr") -> list[Observation]:
    now = datetime.now(timezone.utc)
    return [Observation(radio=Radio.GSM, source="sdr-kal", timestamp=now, serving=False, device=device,
                        channel=c.arfcn, notes=f"offset {c.offset_khz:+.3f} kHz, power {c.power:.0f}") for c in channels]


# --------------------------------------------------------------------------- gr-gsm

_GRGSM = re.compile(
    r"ARFCN:\s*(\d+),\s*Freq:\s*([\d.]+)M,\s*CID:\s*(\d+),\s*LAC:\s*(\d+),\s*MCC:\s*(\d+),\s*MNC:\s*(\d+),\s*Pwr:\s*(-?[\d.]+)")
_NEIGH = re.compile(r"Neighbour Cells:\s*([\d,\s]+)")


def parse_grgsm(text: str) -> list[Observation]:
    now = datetime.now(timezone.utc)
    out: list[Observation] = []
    for line in text.splitlines():
        m = _GRGSM.search(line)
        if m:
            arfcn, _freq, cid, lac, mcc, mnc, pwr = m.groups()
            out.append(Observation(radio=Radio.GSM, source="sdr-gsm", timestamp=now, serving=False,
                                   channel=int(arfcn), cell_id=int(cid), area_code=int(lac), mcc=int(mcc),
                                   mnc=int(mnc), rel_power=float(pwr)))
            continue
        n = _NEIGH.search(line)
        if n and out:
            neigh = [x.strip() for x in n.group(1).split(",") if x.strip()]
            out[-1].notes = "neighbours: " + ", ".join(neigh)
    return out


def grgsm_scan(band: str = "GSM900", gain: float | None = None, args: str | None = None, timeout: float = 300) -> list[Observation]:
    cmd = ["grgsm_scanner", "-b", band]
    if gain is not None:
        cmd += ["-g", str(gain)]
    if args:
        cmd += ["--args", args]
    return parse_grgsm(_run(cmd, timeout))


# --------------------------------------------------------------------------- srsRAN cell_search

_SRS_FOUND = re.compile(
    r"Found CELL\s+([\d.]+)\s*MHz,\s*EARFCN=(\d+),\s*PHYID=(\d+),\s*(\d+)\s*PRB,\s*(\d+)\s*ports?,\s*PSS power=(-?[\d.]+)\s*dBm",
    re.IGNORECASE)
_SRS_SUMMARY = re.compile(r"EARFCN=(\d+)\s+f=([\d.]+)\s*MHz,\s*PCI=(\d+),\s*PRB=(\d+),\s*Ports=(\d+)(?:,\s*CFO=(-?[\d.]+)\s*KHz)?",
                          re.IGNORECASE)
_PRB_MHZ = {6: 1.4, 15: 3.0, 25: 5.0, 50: 10.0, 75: 15.0, 100: 20.0}


def parse_srsran(text: str) -> list[Observation]:
    now = datetime.now(timezone.utc)
    found: dict[tuple[int, int], Observation] = {}
    for m in _SRS_FOUND.finditer(text):
        _f, earfcn, pci, prb, ports, power = m.groups()
        found[(int(earfcn), int(pci))] = Observation(
            radio=Radio.LTE, source="sdr-srsran", timestamp=now, serving=False, channel=int(earfcn), pci=int(pci),
            bandwidth_mhz=_PRB_MHZ.get(int(prb)), rel_power=float(power), notes=f"{ports} antenna ports")
    for m in _SRS_SUMMARY.finditer(text):
        earfcn, _f, pci, prb, ports, cfo = m.groups()
        key = (int(earfcn), int(pci))
        o = found.get(key) or Observation(radio=Radio.LTE, source="sdr-srsran", timestamp=now, serving=False,
                                          channel=int(earfcn), pci=int(pci), bandwidth_mhz=_PRB_MHZ.get(int(prb)),
                                          notes=f"{ports} antenna ports")
        if cfo:
            o.notes = f"{o.notes}, CFO {cfo} kHz"
        found[key] = o
    return list(found.values())


def srsran_cell_search(band: int, earfcn_start: int | None = None, earfcn_end: int | None = None,
                       gain: float | None = None, device_args: str | None = None, timeout: float = 600) -> list[Observation]:
    cmd = ["cell_search", "-b", str(band)]
    if earfcn_start is not None:
        cmd += ["-s", str(earfcn_start)]
    if earfcn_end is not None:
        cmd += ["-e", str(earfcn_end)]
    if gain is not None:
        cmd += ["-g", str(gain)]
    if device_args:
        cmd += ["-a", device_args]
    return parse_srsran(_run(cmd, timeout))


# --------------------------------------------------------------------------- LTE-Cell-Scanner

_LCS_ROW = re.compile(r"^\s*(\d+)\s+(\d)\s+([\d.]+)M\s+(-?[\d.]+)k\s+(-?[\d.]+)\s+([NE])\s+(\d+)\s")


def parse_lte_cell_scanner(text: str) -> list[Observation]:
    from .. import spectrum

    now = datetime.now(timezone.utc)
    out = []
    for line in text.splitlines():
        m = _LCS_ROW.match(line)
        if not m:
            continue
        pci, ports, fc, foff, rxpwr, _cp, nrb = m.groups()
        band = next((b for b in spectrum.LTE_BANDS.values() if b.dl_low <= float(fc) <= b.dl_high), None)
        out.append(Observation(radio=Radio.LTE, source="sdr-lcs", timestamp=now, serving=False, pci=int(pci),
                               channel=band.earfcn(float(fc)) if band else None, rel_power=float(rxpwr),
                               bandwidth_mhz=_PRB_MHZ.get(int(nrb)), notes=f"{ports} ports, offset {foff} kHz"))
    return out
