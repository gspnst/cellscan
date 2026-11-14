"""Qualcomm modems via libqmi: `qmicli -d /dev/cdc-wdm0 -p --nas-get-cell-location-info`.

Gives the serving LTE cell (PLMN, TAC, global cell ID, EARFCN, PCI) plus intra- and inter-frequency
neighbours with RSRP/RSRQ/RSSI, timing advance and – on newer firmware – the 5G NR cell.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ..models import Observation, Radio
from .base import Runner, int_or_none, num, run

_BLOCK = re.compile(r"^(Cell|Frequency) \[(\d+)\]:?$")
_QUOTED = re.compile(r"'([^']*)'")


def _value(raw: str) -> str:
    m = _QUOTED.search(raw)
    return (m.group(1) if m else raw).strip()


def _radio_for(section: str) -> Radio:
    if "wcdma" in section or "umts" in section:
        return Radio.UMTS
    if "gsm" in section:
        return Radio.GSM
    if "5gnr" in section or "nr5g" in section:
        return Radio.NR_NSA
    return Radio.LTE


def parse_cell_location(text: str) -> list[Observation]:
    """Parses qmicli's indented text output into serving + neighbour observations."""
    now = datetime.now(timezone.utc)
    section = ""
    serving: Observation | None = None
    nr: Observation | None = None
    neighbours: list[Observation] = []
    current: Observation | None = None
    freq_channel: int | None = None
    ta: int | None = None

    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("["):
            continue
        block = _BLOCK.match(s)
        if block:
            if block.group(1) == "Frequency":
                freq_channel, current = None, None
            else:
                channel = freq_channel if freq_channel is not None else (serving.channel if serving and "intra" in section else None)
                current = Observation(radio=_radio_for(section), source="qmi", timestamp=now, serving=False, channel=channel)
                neighbours.append(current)
            continue
        if ":" not in s or s.endswith(":"):
            section, current, freq_channel = s.rstrip(":").lower(), None, None
            continue
        key, value = s.split(":", 1)
        key, value = key.strip().lower(), _value(value)

        if "timing advance" in key:
            ta = int_or_none(value)
            continue
        if section.startswith(("5gnr", "nr5g")) or key in ("nr5g arfcn", "nr arfcn"):
            nr = nr or Observation(radio=Radio.NR_NSA, source="qmi", timestamp=now, serving=True)
            _apply(nr, key, value)
            continue
        if current is not None:
            _apply(current, key, value)
            continue
        if key.startswith(("eutra absolute rf channel number", "uarfcn", "arfcn")):
            freq_channel = int_or_none(value)
        if section.startswith("intrafrequency lte info"):
            serving = serving or Observation(radio=Radio.LTE, source="qmi", timestamp=now, serving=True)
            _apply(serving, key, value)

    out: list[Observation] = []
    if serving is not None:
        serving.timing_advance = ta
        for n in list(neighbours):
            if n.pci == serving.pci and n.channel == serving.channel and n.radio is Radio.LTE:
                serving.rsrp, serving.rsrq, serving.rssi = n.rsrp, n.rsrq, n.rssi
                neighbours.remove(n)
        out.append(serving)
    if nr is not None:
        if serving is None:
            nr.radio = Radio.NR_SA
        else:
            nr.capture_id = serving.capture_id = serving.id
        out.append(nr)
    out.extend(neighbours)
    plmn_from = serving or nr
    for o in out:
        if o.mcc is None and plmn_from is not None and plmn_from.mcc is not None and o.family == plmn_from.family:
            o.mcc, o.mnc, o.mnc_digits = plmn_from.mcc, plmn_from.mnc, plmn_from.mnc_digits
    if nr is not None and nr.mcc is None and serving is not None:
        nr.mcc, nr.mnc, nr.mnc_digits = serving.mcc, serving.mnc, serving.mnc_digits
    return [o for o in out if o.pci is not None or o.cell_id is not None]


def _apply(o: Observation, key: str, value: str) -> None:
    if key == "plmn" and value.isdigit() and len(value) in (5, 6):
        o.mcc, o.mnc, o.mnc_digits = int(value[:3]), int(value[3:]), len(value) - 3
    elif key in ("tracking area code", "location area code"):
        o.area_code = int_or_none(value)
    elif key in ("global cell id", "cell id"):
        o.cell_id = int_or_none(value)
    elif key.startswith(("eutra absolute rf channel number", "uarfcn", "arfcn", "nr5g arfcn", "nr arfcn")):
        o.channel = int_or_none(value)
    elif key in ("serving cell id", "physical cell id", "psc", "bsic id"):
        o.pci = int_or_none(value)
    elif key == "rsrp":
        o.rsrp = num(value)
    elif key == "rsrq":
        o.rsrq = num(value)
    elif key == "rssi":
        o.rssi = num(value)
    elif key in ("snr", "sinr"):
        o.sinr = num(value)


class QMISource:
    name = "qmi"

    def __init__(self, device: str = "/dev/cdc-wdm0", runner: Runner = run, use_proxy: bool = True):
        self.device = device
        self.runner = runner
        self.use_proxy = use_proxy

    def poll(self) -> list[Observation]:
        cmd = ["qmicli", "-d", self.device]
        if self.use_proxy:
            cmd.append("-p")  # share the device with ModemManager through qmi-proxy
        cmd.append("--nas-get-cell-location-info")
        cells = parse_cell_location(self.runner(cmd, 15))
        for c in cells:
            c.device = f"qmi:{self.device}"
        return cells
