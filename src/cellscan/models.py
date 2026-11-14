"""Core data model shared by all sources, analysis and exporters."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Family(str, Enum):
    """Radio-access family. Cells are only comparable within one family."""

    GSM = "gsm"
    UMTS = "umts"
    LTE = "lte"
    NR = "nr"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {"gsm": "GSM", "umts": "UMTS", "lte": "LTE", "nr": "5G NR", "unknown": "?"}[self.value]

    @property
    def opencellid_radio(self) -> str | None:
        return {"gsm": "GSM", "umts": "UMTS", "lte": "LTE", "nr": "NR"}.get(self.value)


class Radio(str, Enum):
    """Radio access technology as reported by a modem or detected by the SDR."""

    GSM = "gsm"
    GPRS = "gprs"
    EDGE = "edge"
    UMTS = "umts"
    HSPA = "hspa"
    LTE = "lte"
    NR_NSA = "nr_nsa"
    NR_SA = "nr_sa"
    UNKNOWN = "unknown"

    @property
    def family(self) -> Family:
        return {
            "gsm": Family.GSM, "gprs": Family.GSM, "edge": Family.GSM,
            "umts": Family.UMTS, "hspa": Family.UMTS,
            "lte": Family.LTE,
            "nr_nsa": Family.NR, "nr_sa": Family.NR,
        }.get(self.value, Family.UNKNOWN)

    @property
    def generation(self) -> int:
        return {Family.GSM: 2, Family.UMTS: 3, Family.LTE: 4, Family.NR: 5}.get(self.family, 0)

    @property
    def label(self) -> str:
        return {
            "gsm": "GSM", "gprs": "GPRS", "edge": "EDGE", "umts": "UMTS", "hspa": "HSPA",
            "lte": "LTE", "nr_nsa": "5G NSA", "nr_sa": "5G SA", "unknown": "unknown",
        }[self.value]

    @classmethod
    def parse(cls, text: str | None) -> Radio:
        """Maps the many spellings used by ModemManager, qmicli and AT commands."""
        if not text:
            return cls.UNKNOWN
        t = text.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        table = {
            "gsm": cls.GSM, "gprs": cls.GPRS, "edge": cls.EDGE, "egprs": cls.EDGE,
            "umts": cls.UMTS, "wcdma": cls.UMTS, "utran": cls.UMTS, "hsdpa": cls.HSPA, "hsupa": cls.HSPA,
            "hspa": cls.HSPA, "hspaplus": cls.HSPA, "hspa+": cls.HSPA, "tdscdma": cls.UMTS,
            "lte": cls.LTE, "eutran": cls.LTE, "lteca": cls.LTE, "ltecatm": cls.LTE, "ltenbiot": cls.LTE,
            "5gnr": cls.NR_SA, "nr5g": cls.NR_SA, "nr": cls.NR_SA, "nr5gsa": cls.NR_SA, "5gsa": cls.NR_SA,
            "nr5gnsa": cls.NR_NSA, "5gnsa": cls.NR_NSA, "endc": cls.NR_NSA, "ltenr": cls.NR_NSA,
        }
        return table.get(t, cls.UNKNOWN)


@dataclass(frozen=True)
class GeoPoint:
    lat: float
    lon: float
    accuracy: float | None = None

    @property
    def valid(self) -> bool:
        return -90 <= self.lat <= 90 and -180 <= self.lon <= 180 and not (self.lat == 0 and self.lon == 0)


@dataclass(frozen=True, order=True)
class CellKey:
    """Stable identity of a cell: PLMN + cell ID, or (channel, PCI) when the global ID is unknown."""

    family: str
    mcc: int | None = None
    mnc: int | None = None
    cell_id: int | None = None
    channel: int | None = None
    pci: int | None = None

    @property
    def is_global(self) -> bool:
        return self.cell_id is not None

    def __str__(self) -> str:
        parts = [Family(self.family).label]
        if self.mcc is not None and self.mnc is not None:
            parts.append(format_plmn(self.mcc, self.mnc))
        if self.cell_id is not None:
            parts.append(f"CID {self.cell_id}")
        else:
            if self.channel is not None:
                parts.append(f"CH {self.channel}")
            if self.pci is not None:
                parts.append(f"PCI {self.pci}")
        return " · ".join(parts)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Observation:
    """One measurement of a cell (serving or neighbour) or of the radio state at a moment in time."""

    radio: Radio = Radio.UNKNOWN
    source: str = "manual"
    timestamp: datetime = field(default_factory=_now)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    serving: bool = True
    device: str | None = None
    capture_id: str | None = None

    mcc: int | None = None
    mnc: int | None = None
    mnc_digits: int | None = None
    area_code: int | None = None  # TAC (LTE/NR) or LAC (GSM/UMTS)
    cell_id: int | None = None  # ECI / NCI / UTRAN CID / GSM CI
    pci: int | None = None  # PCI (LTE/NR), PSC (UMTS), BSIC (GSM)

    channel: int | None = None  # EARFCN / NR-ARFCN / UARFCN / ARFCN (downlink)
    band: int | None = None
    bandwidth_mhz: float | None = None

    rsrp: float | None = None
    rsrq: float | None = None
    sinr: float | None = None
    rssi: float | None = None
    timing_advance: int | None = None
    rel_power: float | None = None  # uncalibrated SDR power (dBFS); not graded like RSRP/RSSI

    lat: float | None = None
    lon: float | None = None
    accuracy: float | None = None
    notes: str | None = None

    # ---- derived ---------------------------------------------------------

    @property
    def family(self) -> Family:
        return self.radio.family

    @property
    def location(self) -> GeoPoint | None:
        if self.lat is None or self.lon is None:
            return None
        p = GeoPoint(self.lat, self.lon, self.accuracy)
        return p if p.valid else None

    def set_location(self, point: GeoPoint | None) -> None:
        if point is None:
            self.lat = self.lon = self.accuracy = None
        else:
            self.lat, self.lon, self.accuracy = point.lat, point.lon, point.accuracy

    @property
    def has_signal(self) -> bool:
        return any(v is not None for v in (self.rsrp, self.rsrq, self.sinr, self.rssi))

    @property
    def key(self) -> CellKey | None:
        fam = self.family
        if fam is Family.UNKNOWN:
            return None
        if self.cell_id is not None:
            return CellKey(fam.value, self.mcc, self.mnc, self.cell_id)
        if self.channel is not None and self.pci is not None:
            return CellKey(fam.value, self.mcc, self.mnc, None, self.channel, self.pci)
        return None

    @property
    def plmn(self) -> str | None:
        if self.mcc is None or self.mnc is None:
            return None
        return format_plmn(self.mcc, self.mnc, self.mnc_digits)

    # ---- serialization ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["radio"] = self.radio.value
        d["timestamp"] = self.timestamp.isoformat()
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Observation:
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["radio"] = Radio(data.get("radio", "unknown"))
        ts = data.get("timestamp")
        if isinstance(ts, str):
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            data["timestamp"] = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        elif isinstance(ts, (int, float)):
            data["timestamp"] = datetime.fromtimestamp(ts, timezone.utc)
        return cls(**data)


THREE_DIGIT_MNC_MCCS = {302, 310, 311, 312, 313, 314, 315, 316, 334, 338, 342, 344, 346, 348,
                        352, 354, 356, 358, 360, 365, 376, 405, 708, 722, 732, 750}


def format_plmn(mcc: int, mnc: int, mnc_digits: int | None = None) -> str:
    digits = mnc_digits or (3 if (mcc in THREE_DIGIT_MNC_MCCS or mnc > 99) else 2)
    return f"{mcc:03d}-{mnc:0{digits}d}"


def parse_plmn(text: str) -> tuple[int, int, int] | None:
    """'23003' / '230-03' / '310 260' -> (mcc, mnc, mnc_digits)."""
    parts = [p for p in "".join(c if c.isdigit() else " " for c in text).split() if p]
    if len(parts) == 2 and len(parts[0]) == 3 and len(parts[1]) in (2, 3):
        return int(parts[0]), int(parts[1]), len(parts[1])
    if len(parts) == 1 and len(parts[0]) in (5, 6):
        s = parts[0]
        return int(s[:3]), int(s[3:]), len(s) - 3
    return None


def is_test_plmn(mcc: int, mnc: int) -> bool:
    return (mcc == 1 and mnc in (0, 1, 10)) or (mcc == 999 and mnc in (99, 999)) or mcc == 0
