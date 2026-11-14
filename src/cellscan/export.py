"""CSV / JSON / KML export and JSON(L) import."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from . import identity, operators, signal, spectrum
from .analysis import Cell
from .models import Observation

CSV_COLUMNS = [
    "timestamp", "source", "device", "serving", "radio", "mcc", "mnc", "operator", "area_code", "cell_id",
    "site_id", "local_cell_id", "pci", "channel", "band", "dl_mhz", "bandwidth_mhz", "rsrp_dbm", "rsrq_db",
    "sinr_db", "rssi_dbm", "rel_power_dbfs", "timing_advance", "latitude", "longitude", "accuracy_m", "notes",
]


def to_csv(observations: Iterable[Observation], gnb_bits: int = identity.DEFAULT_GNB_BITS) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for o in sorted(observations, key=lambda o: o.timestamp):
        dec = identity.decode(o.family, o.cell_id, o.mcc, o.mnc, gnb_bits) if o.cell_id is not None else None
        info = spectrum.channel_info(o.family, o.channel, o.mcc, o.band) if o.channel is not None else None
        w.writerow([
            o.timestamp.isoformat(), o.source, o.device or "", int(o.serving), o.radio.value,
            "" if o.mcc is None else f"{o.mcc:03d}", "" if o.mnc is None else o.mnc,
            operators.describe(o.mcc, o.mnc) if o.mcc is not None else "",
            _s(o.area_code), _s(o.cell_id), _s(dec.site_id if dec else None), _s(dec.local_id if dec else None),
            _s(o.pci), _s(o.channel), _s(o.band if o.band is not None else (info.band if info else None)),
            _s(info.dl_mhz if info else None), _s(o.bandwidth_mhz), _s(o.rsrp), _s(o.rsrq), _s(o.sinr), _s(o.rssi),
            _s(o.rel_power), _s(o.timing_advance), _s(o.lat), _s(o.lon), _s(o.accuracy), o.notes or "",
        ])
    return buf.getvalue()


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.7f}".rstrip("0").rstrip(".")
    return str(v)


def to_json(observations: Iterable[Observation]) -> str:
    return json.dumps({
        "format": "cellscan", "version": 1, "exported_at": datetime.now(timezone.utc).isoformat(),
        "observations": [o.to_dict() for o in observations],
    }, indent=2)


def load(text: str) -> list[Observation]:
    """Reads a JSON export, a bare JSON array, or JSON Lines (session files)."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [Observation.from_dict(json.loads(line)) for line in text.splitlines() if line.strip()]
    if isinstance(data, dict) and data.get("format") == "cell-analyzer":
        return [from_ios(d) for d in data.get("observations", [])]
    if isinstance(data, dict) and "observations" in data:
        return [Observation.from_dict(d) for d in data["observations"]]
    if isinstance(data, list):
        return [Observation.from_dict(d) for d in data]
    return [Observation.from_dict(data)]


_IOS_RADIO = {
    "gprs": "gprs", "edge": "edge", "gsm": "gsm", "umts": "umts", "hsdpa": "hspa", "hsupa": "hspa",
    "lte": "lte", "nrNSA": "nr_nsa", "nrSA": "nr_sa",
}
_IOS_FIELDS = {
    "id": "id", "timestamp": "timestamp", "mcc": "mcc", "mnc": "mnc", "mncDigits": "mnc_digits",
    "areaCode": "area_code", "cellId": "cell_id", "pci": "pci", "channel": "channel", "band": "band",
    "bandwidthMHz": "bandwidth_mhz", "rsrp": "rsrp", "rsrq": "rsrq", "sinr": "sinr", "rssi": "rssi",
    "timingAdvance": "timing_advance", "notes": "notes", "captureID": "capture_id", "simSlot": "device",
}


def from_ios(d: dict) -> Observation:
    """Maps an observation exported by the iOS Cell Analyzer app (camelCase JSON)."""
    data = {dst: d[src] for src, dst in _IOS_FIELDS.items() if d.get(src) is not None}
    data["radio"] = _IOS_RADIO.get(d.get("radio", ""), "unknown")
    data["source"] = "ios-" + str(d.get("source", "import"))
    data["serving"] = True
    loc = d.get("location") or {}
    if "latitude" in loc and "longitude" in loc:
        data["lat"], data["lon"] = loc["latitude"], loc["longitude"]
        data["accuracy"] = loc.get("horizontalAccuracy")
    if isinstance(data.get("id"), str):
        data["id"] = data["id"].replace("-", "").lower()
    return Observation.from_dict(data)


_KML_STYLES = {
    signal.Grade.EXCELLENT: "ff00c000", signal.Grade.GOOD: "ff00d0a0", signal.Grade.FAIR: "ff00c0ff",
    signal.Grade.POOR: "ff0060ff", signal.Grade.VERY_POOR: "ff0000c0",
}


def to_kml(observations: Iterable[Observation], cells: Iterable[Cell] = (), gnb_bits: int = identity.DEFAULT_GNB_BITS) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>cellscan export</name>']
    for g, color in _KML_STYLES.items():
        parts.append(f'<Style id="{g.name.lower()}"><IconStyle><color>{color}</color><scale>0.6</scale></IconStyle></Style>')
    parts.append('<Style id="none"><IconStyle><color>ff888888</color><scale>0.5</scale></IconStyle></Style>')
    parts.append('<Style id="tower"><IconStyle><color>ffff6000</color><scale>1.1</scale></IconStyle></Style>')
    parts.append("<Folder><name>Measurements</name>")
    for o in sorted(observations, key=lambda o: o.timestamp):
        p = o.location
        if p is None:
            continue
        g = signal.overall(o)
        desc = f"{o.radio.label} {o.key or ''} " + (f"RSRP {o.rsrp:.0f} dBm" if o.rsrp is not None else "")
        parts.append(f"<Placemark><name>{escape(o.radio.label)}</name><TimeStamp><when>{o.timestamp.isoformat()}</when></TimeStamp>"
                     f"<styleUrl>#{g.name.lower() if g is not None else 'none'}</styleUrl><description>{escape(desc.strip())}</description>"
                     f"<Point><coordinates>{p.lon},{p.lat},0</coordinates></Point></Placemark>")
    parts.append("</Folder><Folder><name>Estimated cell positions</name>")
    for c in cells:
        if c.estimate is None:
            continue
        desc = f"{c.key} · {len(c.observations)} samples" + (f" · {c.band_label}" if c.band_label else "")
        parts.append(f"<Placemark><name>{escape(c.title(gnb_bits))}</name><styleUrl>#tower</styleUrl>"
                     f"<description>{escape(desc)}</description><Point><coordinates>{c.estimate.lon},{c.estimate.lat},0</coordinates></Point></Placemark>")
    parts.append("</Folder></Document></kml>")
    return "\n".join(parts) + "\n"
