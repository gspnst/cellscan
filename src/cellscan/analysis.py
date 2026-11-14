"""Cell aggregation, session statistics, event extraction and anomaly heuristics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean

from . import geo, identity, operators, signal, spectrum
from .models import CellKey, Family, GeoPoint, Observation, Radio, format_plmn, is_test_plmn

MAX_SAMPLE_GAP_S = 300


@dataclass
class Stats:
    count: int
    min: float
    max: float
    mean: float
    latest: float

    @classmethod
    def of(cls, values: list[float]) -> Stats | None:
        if not values:
            return None
        return cls(len(values), min(values), max(values), fmean(values), values[-1])


@dataclass
class Cell:
    key: CellKey
    radio: Radio
    observations: list[Observation]
    mcc: int | None = None
    mnc: int | None = None
    area_code: int | None = None
    cell_id: int | None = None
    pci: int | None = None
    channel: int | None = None
    band: int | None = None
    bandwidth_mhz: float | None = None
    rsrp: Stats | None = None
    rsrq: Stats | None = None
    sinr: Stats | None = None
    rssi: Stats | None = None
    rel_power: Stats | None = None
    estimate: GeoPoint | None = None
    sources: set[str] = field(default_factory=set)
    served: bool = False  # was ever the serving cell

    @property
    def family(self) -> Family:
        return Family(self.key.family)

    @property
    def first_seen(self) -> datetime:
        return self.observations[0].timestamp

    @property
    def last_seen(self) -> datetime:
        return self.observations[-1].timestamp

    @property
    def operator(self) -> str:
        return operators.describe(self.mcc, self.mnc)

    @property
    def channel_info(self) -> spectrum.ChannelInfo | None:
        if self.channel is None:
            return None
        return spectrum.channel_info(self.family, self.channel, self.mcc, self.band)

    @property
    def band_label(self) -> str | None:
        info = self.channel_info
        if info and info.band_label:
            return info.band_label
        if self.band is not None:
            return f"n{self.band}" if self.family is Family.NR else f"B{self.band}"
        return None

    def decoded(self, gnb_bits: int = identity.DEFAULT_GNB_BITS) -> identity.DecodedIdentity | None:
        if self.cell_id is None:
            return None
        return identity.decode(self.family, self.cell_id, self.mcc, self.mnc, gnb_bits)

    def title(self, gnb_bits: int = identity.DEFAULT_GNB_BITS) -> str:
        d = self.decoded(gnb_bits)
        if d and d.site_id is not None:
            prefix = {Family.LTE: "eNB", Family.NR: "gNB", Family.UMTS: "RNC"}.get(self.family, "Site")
            return f"{prefix} {d.site_id}·{d.local_id}"
        if d:
            return f"CID {d.raw}"
        parts = []
        if self.pci is not None:
            parts.append(f"PCI {self.pci}")
        if self.channel is not None:
            parts.append(f"@{self.channel}")
        return " ".join(parts) or str(self.key)

    @property
    def grade(self) -> signal.Grade | None:
        grades = []
        if self.rsrp:
            grades.append(signal.grade("rsrp", self.rsrp.mean, self.family))
        if self.sinr:
            grades.append(signal.grade("sinr", self.sinr.mean, self.family))
        if not grades and self.rssi:
            grades.append(signal.grade("rssi", self.rssi.mean, self.family))
        return min(grades) if grades else None


def _plmn_compatible(key: CellKey, o: Observation) -> bool:
    return (key.mcc is None or o.mcc is None or key.mcc == o.mcc) and (key.mnc is None or o.mnc is None or key.mnc == o.mnc)


def aggregate(observations: Iterable[Observation]) -> list[Cell]:
    """Groups observations into cells; channel+PCI-only sightings merge into a global cell when unambiguous."""
    obs = sorted(observations, key=lambda o: o.timestamp)
    groups: dict[CellKey, list[Observation]] = defaultdict(list)
    physical: list[Observation] = []
    for o in obs:
        key = o.key
        if key is None:
            continue
        (groups[key] if key.is_global else physical).append(o)

    for key in [k for k in groups if k.mcc is None]:
        matches = [k for k in groups if k.mcc is not None and k.family == key.family and k.cell_id == key.cell_id]
        if len(matches) == 1:
            groups[matches[0]].extend(groups.pop(key))

    for o in physical:
        matches = [k for k, lst in groups.items()
                   if k.is_global and k.family == o.family.value and _plmn_compatible(k, o)
                   and any(x.channel == o.channel and x.pci == o.pci for x in lst)]
        target = matches[0] if len(matches) == 1 else o.key
        groups[target].append(o)

    cells = [_build(key, sorted(lst, key=lambda o: o.timestamp)) for key, lst in groups.items()]
    return sorted(cells, key=lambda c: c.last_seen, reverse=True)


def _build(key: CellKey, lst: list[Observation]) -> Cell:
    def latest(attr: str):
        for o in reversed(lst):
            v = getattr(o, attr)
            if v is not None:
                return v
        return None

    located = [(o.location, o.rsrp if o.rsrp is not None else o.rssi) for o in lst if o.location]
    return Cell(
        key=key, radio=lst[-1].radio, observations=lst,
        mcc=latest("mcc"), mnc=latest("mnc"), area_code=latest("area_code"), cell_id=latest("cell_id"),
        pci=latest("pci"), channel=latest("channel"), band=latest("band"), bandwidth_mhz=latest("bandwidth_mhz"),
        rsrp=Stats.of([o.rsrp for o in lst if o.rsrp is not None]),
        rsrq=Stats.of([o.rsrq for o in lst if o.rsrq is not None]),
        sinr=Stats.of([o.sinr for o in lst if o.sinr is not None]),
        rssi=Stats.of([o.rssi for o in lst if o.rssi is not None]),
        rel_power=Stats.of([o.rel_power for o in lst if o.rel_power is not None]),
        estimate=geo.weighted_centroid(located), sources={o.source for o in lst},
        served=any(o.serving for o in lst),
    )


# --------------------------------------------------------------------------- events & summary


@dataclass
class Event:
    timestamp: datetime
    kind: str  # radio | handover | area
    text: str
    device: str | None = None


def radio_timeline(lst: list[Observation]) -> list[Observation]:
    """Serving observations that describe 'which technology was active' (one per capture)."""
    out, seen = [], set()
    for o in lst:
        if not o.serving or o.radio is Radio.UNKNOWN:
            continue
        if o.capture_id:
            if o.capture_id in seen:
                continue
            seen.add(o.capture_id)
            legs = [x for x in lst if x.capture_id == o.capture_id and x.serving]
            o = max(legs, key=lambda x: x.radio.generation)
        out.append(o)
    return out


def _same_cell(a: CellKey, b: CellKey) -> bool:
    """Equal identities, treating a missing PLMN as a wildcard."""
    if a.family != b.family or a.cell_id != b.cell_id or a.channel != b.channel or a.pci != b.pci:
        return False
    return (a.mcc is None or b.mcc is None or a.mcc == b.mcc) and (a.mnc is None or b.mnc is None or a.mnc == b.mnc)


def events(observations: Iterable[Observation]) -> list[Event]:
    obs = sorted(observations, key=lambda o: o.timestamp)
    by_device: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        if o.source.startswith("modem") or o.source in ("qmi", "quectel", "manual", "demo"):
            by_device[o.device or ""].append(o)
    out: list[Event] = []
    for device, lst in by_device.items():
        last_radio = None
        for o in radio_timeline(lst):
            if last_radio is not None and o.radio != last_radio:
                out.append(Event(o.timestamp, "radio", f"{last_radio.label} → {o.radio.label}", device or None))
            last_radio = o.radio
        last_cell: dict[Family, CellKey] = {}
        last_area: dict[Family, int] = {}
        for o in lst:
            if not o.serving:
                continue
            key = o.key
            if key is not None and key.is_global:
                prev = last_cell.get(o.family)
                if prev is not None and not _same_cell(prev, key):
                    dec = identity.decode(o.family, key.cell_id, o.mcc, o.mnc)  # type: ignore[arg-type]
                    where = f" · PCI {o.pci}" if o.pci is not None else ""
                    out.append(Event(o.timestamp, "handover", f"Handover to {o.family.label} {dec.short}{where}", device or None))
                last_cell[o.family] = key
            if o.area_code is not None:
                prev_area = last_area.get(o.family)
                if prev_area is not None and prev_area != o.area_code:
                    out.append(Event(o.timestamp, "area", f"Area {prev_area} → {o.area_code}", device or None))
                last_area[o.family] = o.area_code
    return sorted(out, key=lambda e: e.timestamp)


@dataclass
class Summary:
    observations: int
    cells: int
    serving_cells: int
    handovers: int
    radio_changes: int
    start: datetime | None
    end: datetime | None
    time_by_radio: dict[Radio, float]
    bands: dict[str, int]
    operators: dict[str, int]
    rsrp: Stats | None
    sinr: Stats | None
    events: list[Event]

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds() if self.start and self.end else 0.0

    @property
    def share_5g(self) -> float:
        total = sum(self.time_by_radio.values())
        return sum(t for r, t in self.time_by_radio.items() if r.family is Family.NR) / total if total else 0.0


def summarize(observations: Iterable[Observation], cells: list[Cell] | None = None) -> Summary:
    obs = sorted(observations, key=lambda o: o.timestamp)
    cells = cells if cells is not None else aggregate(obs)
    evs = events(obs)
    time_by_radio: dict[Radio, float] = defaultdict(float)
    by_device: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_device[o.device or ""].append(o)
    for lst in by_device.values():
        tl = radio_timeline(lst)
        for a, b in zip(tl, tl[1:]):
            dt = (b.timestamp - a.timestamp).total_seconds()
            if 0 < dt <= MAX_SAMPLE_GAP_S:
                time_by_radio[a.radio] += dt
    bands: dict[str, int] = defaultdict(int)
    ops: dict[str, int] = defaultdict(int)
    for c in cells:
        if c.band_label:
            bands[c.band_label] += len(c.observations)
        if c.mcc is not None:
            ops[c.operator] += len(c.observations)
    return Summary(
        observations=len(obs), cells=len(cells), serving_cells=sum(1 for c in cells if c.served),
        handovers=sum(1 for e in evs if e.kind == "handover"),
        radio_changes=sum(1 for e in evs if e.kind == "radio"),
        start=obs[0].timestamp if obs else None, end=obs[-1].timestamp if obs else None,
        time_by_radio=dict(time_by_radio), bands=dict(bands), operators=dict(ops),
        rsrp=Stats.of([o.rsrp for o in obs if o.rsrp is not None and o.serving]),
        sinr=Stats.of([o.sinr for o in obs if o.sinr is not None and o.serving]),
        events=evs,
    )


# --------------------------------------------------------------------------- anomalies


@dataclass
class Anomaly:
    kind: str
    severity: int  # 0 info, 1 low, 2 medium, 3 high
    title: str
    detail: str
    timestamp: datetime
    key: CellKey | None = None

    @property
    def severity_label(self) -> str:
        return ["info", "low", "medium", "high"][self.severity]


@dataclass
class DetectorConfig:
    strong_signal_dbm: float = -50
    pci_collision_radius_m: float = 2_000
    transient_area_window_s: float = 15 * 60
    max_range_m: float = 35_000


def detect_anomalies(observations: Iterable[Observation], towers: dict[CellKey, GeoPoint] | None = None,
                     not_in_db: set[CellKey] | None = None, config: DetectorConfig | None = None) -> list[Anomaly]:
    cfg = config or DetectorConfig()
    obs = sorted(observations, key=lambda o: o.timestamp)
    out: list[Anomaly] = []

    seen_plmn: set[tuple[int, int]] = set()
    for o in obs:
        if o.mcc is None or o.mnc is None or (o.mcc, o.mnc) in seen_plmn:
            continue
        seen_plmn.add((o.mcc, o.mnc))
        plmn = format_plmn(o.mcc, o.mnc, o.mnc_digits)
        if is_test_plmn(o.mcc, o.mnc):
            out.append(Anomaly("test_network", 3, f"Test network {plmn}",
                               "Reserved for lab equipment; never broadcast by commercial networks but common on cheap cell-site simulators.",
                               o.timestamp, o.key))
        elif not operators.is_known_mcc(o.mcc):
            out.append(Anomaly("unknown_mcc", 2, f"Unallocated country code {o.mcc}",
                               "MCC is not assigned to any country: a decoding error, a misconfigured cell, or not a real network.",
                               o.timestamp, o.key))

    by_device: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_device[o.device or ""].append(o)
    for lst in by_device.values():
        tl = radio_timeline(lst)
        for a, b in zip(tl, tl[1:]):
            if a.radio.generation >= 4 and b.radio.generation == 2:
                out.append(Anomaly("downgrade_2g", 2, f"Dropped from {a.radio.label} to {b.radio.label}",
                                   "2G lacks network authentication; forcing phones to 2G is a classic interception technique. "
                                   "Poor coverage is the innocent explanation – check whether it repeats at the same place.",
                                   b.timestamp, b.key))

    global_groups: dict[CellKey, list[Observation]] = defaultdict(list)
    for o in obs:
        if o.key is not None and o.key.is_global:
            global_groups[o.key].append(o)
    for key, lst in global_groups.items():
        pcis = sorted({o.pci for o in lst if o.pci is not None})
        chans = sorted({o.channel for o in lst if o.channel is not None})
        if len(pcis) > 1 or len(chans) > 1:
            what = []
            if len(pcis) > 1:
                what.append("PCIs " + ", ".join(map(str, pcis)))
            if len(chans) > 1:
                what.append("channels " + ", ".join(map(str, chans)))
            out.append(Anomaly("identity_conflict", 3, "Cell identity reused with different radio parameters",
                               f"{key} seen with {' and '.join(what)}. Real cells keep PCI and frequency; a spoofed copy usually doesn't.",
                               lst[-1].timestamp, key))
        areas = sorted({o.area_code for o in lst if o.area_code is not None})
        if len(areas) > 1:
            out.append(Anomaly("area_conflict", 3, "Cell changed its area code",
                               f"{key} seen in areas {', '.join(map(str, areas))}. A different TAC/LAC forces re-registration, which simulators exploit.",
                               lst[-1].timestamp, key))

    collision_groups: dict[tuple, dict[int, Observation]] = defaultdict(dict)
    for o in obs:
        if o.cell_id is not None and o.channel is not None and o.pci is not None and o.location is not None:
            collision_groups[(o.family, o.mcc, o.mnc, o.channel, o.pci)][o.cell_id] = o
    for (_, _, _, channel, pci), per_cell in collision_groups.items():
        items = list(per_cell.values())
        done = False
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                d = geo.distance(items[i].location, items[j].location)  # type: ignore[arg-type]
                if d <= cfg.pci_collision_radius_m and not done:
                    done = True
                    out.append(Anomaly("pci_collision", 2, f"PCI {pci} on channel {channel} used by two cells {int(d)} m apart",
                                       f"Cells {items[i].cell_id} and {items[j].cell_id} share channel and PCI – a planning error or a fake cell.",
                                       max(items[i].timestamp, items[j].timestamp), items[j].key))

    area_groups: dict[tuple, list[Observation]] = defaultdict(list)
    for o in obs:
        if o.serving and o.area_code is not None and o.mcc is not None:
            area_groups[(o.device, o.family, o.mcc, o.mnc)].append(o)
    for lst in area_groups.values():
        runs: list[list[Observation]] = []
        for o in lst:
            if runs and runs[-1][0].area_code == o.area_code:
                runs[-1].append(o)
            else:
                runs.append([o])
        for before, mid, after in zip(runs, runs[1:], runs[2:]):
            if before[0].area_code == after[0].area_code:
                gap = (after[0].timestamp - before[-1].timestamp).total_seconds()
                if gap <= cfg.transient_area_window_s:
                    out.append(Anomaly("transient_area", 2, f"Brief switch to area code {mid[0].area_code}",
                                       f"Left area {before[0].area_code} and returned within {int(gap // 60) + 1} min – "
                                       "typical of a cell luring phones into a location update.", mid[0].timestamp, mid[0].key))

    seen_strong: set[CellKey] = set()
    for o in obs:
        if o.rsrp is not None and o.rsrp > cfg.strong_signal_dbm and o.key and o.key not in seen_strong:
            seen_strong.add(o.key)
            out.append(Anomaly("strong_signal", 1, f"Very strong signal ({o.rsrp:.0f} dBm)",
                               "Transmitter is very close: small cell, indoor system – or a portable base station.", o.timestamp, o.key))

    by_key: dict[CellKey, list[Observation]] = defaultdict(list)
    for o in obs:
        if o.key:
            by_key[o.key].append(o)
    for key, tower in (towers or {}).items():
        dists = [geo.distance(o.location, tower) for o in by_key.get(key, []) if o.location]
        if dists and min(dists) > cfg.max_range_m:
            out.append(Anomaly("tower_far", 2, f"Measured {min(dists) / 1000:.0f} km from its registered site",
                               f"The database places {key} far beyond normal range. Wrong entry – or the identity is broadcast elsewhere.",
                               by_key[key][-1].timestamp, key))
    for key in not_in_db or set():
        if key in by_key:
            out.append(Anomaly("not_in_db", 1, "Cell not in database",
                               f"{key} is unknown to the cell database; common for new cells, relevant together with other findings.",
                               by_key[key][-1].timestamp, key))

    return sorted(out, key=lambda a: (-a.severity, -a.timestamp.timestamp()))
