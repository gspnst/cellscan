"""Laptop WWAN modem through ModemManager (`mmcli -J`).

Reads, without changing the modem's connection:
  * access technology and operator (`mmcli -m N -J`)
  * 3GPP location: MCC/MNC/LAC/TAC/cell ID (`--location-get`, needs `--location-enable-3gpp` once)
  * extended signal: RSRP/RSRQ/SNR per RAT (`--signal-get`, needs `--signal-setup=<s>` once)
  * serving + neighbour cells (`--get-cell-info`, ModemManager >= 1.20, modem dependent)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..models import Observation, Radio
from .base import Runner, SourceError, int_or_none, num, run


def radio_from_access_technologies(techs: list[str]) -> Radio:
    t = {x.lower() for x in techs}
    if "5gnr" in t and ("lte" in t or "lte-cat-m" in t):
        return Radio.NR_NSA
    if "5gnr" in t:
        return Radio.NR_SA
    for name, radio in (("lte", Radio.LTE), ("lte-cat-m", Radio.LTE), ("lte-nb-iot", Radio.LTE),
                        ("hspa-plus", Radio.HSPA), ("hspa", Radio.HSPA), ("hsdpa", Radio.HSPA), ("hsupa", Radio.HSPA),
                        ("umts", Radio.UMTS), ("edge", Radio.EDGE), ("gprs", Radio.GPRS), ("gsm", Radio.GSM)):
        if name in t:
            return radio
    return Radio.UNKNOWN


@dataclass
class ModemStatus:
    modem: str
    manufacturer: str | None = None
    model: str | None = None
    state: str | None = None
    radio: Radio = Radio.UNKNOWN
    access_technologies: list[str] = field(default_factory=list)
    operator_code: str | None = None
    operator_name: str | None = None
    registration: str | None = None
    signal_quality: int | None = None


def parse_modem(data: dict, modem: str = "0") -> ModemStatus:
    m = data.get("modem", {})
    gen = m.get("generic", {})
    g3 = m.get("3gpp", {})
    techs = [t for t in gen.get("access-technologies", []) if t and t != "--"]
    quality = gen.get("signal-quality", {})
    return ModemStatus(
        modem=modem, manufacturer=_clean(gen.get("manufacturer")), model=_clean(gen.get("model")),
        state=_clean(gen.get("state")), radio=radio_from_access_technologies(techs), access_technologies=techs,
        operator_code=_clean(g3.get("operator-code")), operator_name=_clean(g3.get("operator-name")),
        registration=_clean(g3.get("registration-state")),
        signal_quality=int_or_none(quality.get("value") if isinstance(quality, dict) else quality),
    )


def _clean(v: object) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s in ("", "--") else s


def parse_location(data: dict) -> dict:
    """Returns {mcc, mnc, mnc_digits, area_code, cell_id} from `--location-get -J` (hex lac/tac/cid)."""
    loc = data.get("modem", {}).get("location", {}).get("3gpp", {})
    out: dict = {}
    mcc, mnc = _clean(loc.get("mcc")), _clean(loc.get("mnc"))
    if mcc and mcc.isdigit():
        out["mcc"] = int(mcc)
    if mnc and mnc.isdigit():
        out["mnc"], out["mnc_digits"] = int(mnc), len(mnc)
    tac = int_or_none(_clean(loc.get("tac")), 16)
    lac = int_or_none(_clean(loc.get("lac")), 16)
    area = tac if tac not in (None, 0) else lac
    if area is not None and area not in (0xFFFE, 0xFFFF, 0xFFFFFF):
        out["area_code"] = area
    cid = int_or_none(_clean(loc.get("cid")), 16)
    if cid is not None and cid not in (0, 0xFFFFFFFF):
        out["cell_id"] = cid
    return out


def parse_signal(data: dict) -> dict[str, dict[str, float]]:
    """Returns per-RAT metrics, e.g. {'lte': {'rsrp': -95.0, ...}, 'nr': {...}}."""
    sig = data.get("modem", {}).get("signal", {})
    out: dict[str, dict[str, float]] = {}
    for key, rat in (("lte", "lte"), ("5g", "nr"), ("nr5g", "nr"), ("umts", "umts"), ("gsm", "gsm")):
        block = sig.get(key)
        if not isinstance(block, dict):
            continue
        metrics = {}
        for src, dst in (("rsrp", "rsrp"), ("rsrq", "rsrq"), ("snr", "sinr"), ("sinr", "sinr"), ("rssi", "rssi"),
                         ("rscp", "rsrp"), ("ecio", "rsrq")):
            v = num(block.get(src))
            if v is not None and dst not in metrics:
                metrics[dst] = v
        if metrics:
            out[rat] = metrics
    return out


_CELL_KEYS = {
    "cell type": "type", "serving": "serving", "operator id": "plmn", "ci": "ci", "physical ci": "pci",
    "tac": "tac", "lac": "lac", "earfcn": "channel", "uarfcn": "channel", "arfcn": "channel", "nrarfcn": "channel",
    "rsrp": "rsrp", "rsrq": "rsrq", "sinr": "sinr", "snr": "sinr", "rssi": "rssi", "rscp": "rsrp", "ecio": "rsrq",
    "timing advance": "ta", "bandwidth": "bandwidth", "psc": "pci", "bsic": "pci", "serving cell type": "serving_type",
}


def _cell_fields(entry: object) -> dict[str, str]:
    if isinstance(entry, dict):
        items = entry.items()
    else:
        items = (p.split(":", 1) for p in str(entry).split(",") if ":" in p)
    out = {}
    for k, v in items:
        key = _CELL_KEYS.get(str(k).strip().lower().replace("-", " "))
        if key:
            out[key] = str(v).strip()
    return out


def parse_cell_info(data: dict, mcc_mnc: tuple[int, int, int] | None = None) -> list[Observation]:
    """Parses `--get-cell-info -J`. ModemManager prints CI/TAC/LAC in hex and PCI/channels in decimal."""
    entries = data.get("modem", {}).get("cell-info", []) or []
    now = datetime.now(timezone.utc)
    out = []
    for entry in entries:
        f = _cell_fields(entry)
        radio = {"lte": Radio.LTE, "5gnr": Radio.NR_SA, "nr5g": Radio.NR_SA, "umts": Radio.UMTS, "gsm": Radio.GSM,
                 "tdscdma": Radio.UMTS, "cdma": Radio.UNKNOWN}.get(f.get("type", "").lower(), Radio.UNKNOWN)
        o = Observation(radio=radio, source="modem", timestamp=now, serving=f.get("serving", "no").lower() in ("yes", "true", "1"))
        plmn = f.get("plmn")
        if plmn and plmn.isdigit() and len(plmn) in (5, 6):
            o.mcc, o.mnc, o.mnc_digits = int(plmn[:3]), int(plmn[3:]), len(plmn) - 3
        elif mcc_mnc and o.serving:
            o.mcc, o.mnc, o.mnc_digits = mcc_mnc
        o.cell_id = int_or_none(f.get("ci"), 16)
        o.area_code = int_or_none(f.get("tac") or f.get("lac"), 16)
        o.pci = int_or_none(f.get("pci"))
        o.channel = int_or_none(f.get("channel"))
        o.rsrp, o.rsrq, o.sinr, o.rssi = num(f.get("rsrp")), num(f.get("rsrq")), num(f.get("sinr")), num(f.get("rssi"))
        o.timing_advance = int_or_none(f.get("ta"))
        out.append(o)
    return out


class ModemManagerSource:
    """Polls one modem. `setup()` enables 3GPP location and extended signal reporting (needs root/polkit)."""

    name = "modem"

    def __init__(self, modem: str = "any", runner: Runner = run, signal_rate: int = 5):
        self.modem = modem
        self.runner = runner
        self.signal_rate = signal_rate
        self.status: ModemStatus | None = None
        self.cell_info_supported = True
        self.warnings: list[str] = []

    def _mmcli(self, *args: str, timeout: float = 10) -> dict:
        return json.loads(self.runner(["mmcli", "-m", self.modem, "-J", *args], timeout) or "{}")

    def setup(self) -> None:
        for args in (("--location-enable-3gpp",), (f"--signal-setup={self.signal_rate}",)):
            try:
                self.runner(["mmcli", "-m", self.modem, *args], 10)
            except SourceError as exc:
                self.warnings.append(str(exc))

    def poll(self) -> list[Observation]:
        self.status = parse_modem(self._mmcli(), self.modem)
        radio = self.status.radio
        try:
            loc = parse_location(self._mmcli("--location-get"))
        except SourceError as exc:
            loc = {}
            self._warn(f"location unavailable: {exc}")
        try:
            sig = parse_signal(self._mmcli("--signal-get"))
        except SourceError as exc:
            sig = {}
            self._warn(f"extended signal unavailable: {exc}")

        plmn = (loc["mcc"], loc["mnc"], loc["mnc_digits"]) if "mcc" in loc and "mnc" in loc else None
        cells: list[Observation] = []
        if self.cell_info_supported:
            try:
                cells = parse_cell_info(self._mmcli("--get-cell-info"), plmn)
            except SourceError as exc:
                self.cell_info_supported = False
                self._warn(f"cell info unsupported: {exc}")

        device = f"mm:{self.modem}"
        for c in cells:
            c.device = device
        serving = [c for c in cells if c.serving]
        if not serving:
            # Build the serving observation from location + signal.
            family_key = {Radio.NR_SA: "nr", Radio.NR_NSA: "lte", Radio.LTE: "lte", Radio.UMTS: "umts",
                          Radio.HSPA: "umts"}.get(radio, "gsm")
            o = Observation(radio=Radio.LTE if radio is Radio.NR_NSA else radio, source="modem", device=device, **loc)
            for k, v in sig.get(family_key, {}).items():
                setattr(o, k, v)
            serving = [o]
            cells.insert(0, o)
            if radio is Radio.NR_NSA and "nr" in sig:
                nr = Observation(radio=Radio.NR_NSA, source="modem", device=device, capture_id=o.id,
                                 mcc=loc.get("mcc"), mnc=loc.get("mnc"), mnc_digits=loc.get("mnc_digits"), **sig["nr"])
                o.capture_id = o.id
                cells.insert(1, nr)
        else:
            for o in serving:
                for k, v in sig.get("nr" if o.family.value == "nr" else o.family.value, {}).items():
                    if getattr(o, k) is None:
                        setattr(o, k, v)
                if o.cell_id is None:
                    o.cell_id = loc.get("cell_id")
                if o.area_code is None:
                    o.area_code = loc.get("area_code")
            if radio is Radio.NR_NSA:
                for o in serving:
                    if o.radio is Radio.NR_SA:
                        o.radio = Radio.NR_NSA
        return cells

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)


def list_modems(runner: Runner = run) -> list[str]:
    data = json.loads(runner(["mmcli", "-L", "-J"], 10) or "{}")
    return [p.rsplit("/", 1)[-1] for p in data.get("modem-list", [])]
