"""OpenCellID `cell/get` lookups (https://wiki.opencellid.org/wiki/API). Sends only the cell identity."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .models import Family, GeoPoint

ENDPOINT = "https://opencellid.org/cell/get"


class LookupError_(Exception):
    pass


@dataclass
class Tower:
    location: GeoPoint
    range_m: float | None = None
    samples: int | None = None


def build_url(key: str, mcc: int, mnc: int, area: int, cell_id: int, family: Family) -> str:
    radio = family.opencellid_radio
    if not key.strip():
        raise LookupError_("OpenCellID API key missing (set CELLSCAN_OPENCELLID_KEY or --key)")
    if radio is None:
        raise LookupError_(f"cannot look up {family.label} cells")
    q = urllib.parse.urlencode({"key": key.strip(), "mcc": mcc, "mnc": mnc, "lac": area, "cellid": cell_id,
                                "radio": radio, "format": "json"})
    return f"{ENDPOINT}?{q}"


def parse(body: bytes | str) -> Tower | None:
    """Returns the tower, None when the cell is unknown; raises on service errors."""
    text = body.decode() if isinstance(body, bytes) else body
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        if "not found" in text.lower():
            return None
        raise LookupError_("unexpected response from OpenCellID") from exc
    if "error" in data:
        if "not found" in str(data["error"]).lower() or data.get("code") == 1:
            return None
        raise LookupError_(f"OpenCellID: {data['error']}")
    if "lat" not in data or "lon" not in data:
        raise LookupError_("unexpected response from OpenCellID")
    point = GeoPoint(float(data["lat"]), float(data["lon"]))
    if not point.valid:
        return None
    return Tower(point, data.get("range"), data.get("samples"))


def lookup(key: str, mcc: int, mnc: int, area: int, cell_id: int, family: Family, timeout: float = 15) -> Tower | None:
    url = build_url(key, mcc, mnc, area, cell_id, family)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "cellscan"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https endpoint
        return parse(resp.read())
