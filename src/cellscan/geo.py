"""Geodesy helpers: distances, bearings, timing advance, signal-weighted centroid."""

from __future__ import annotations

import math
from collections.abc import Iterable

from .models import Family, GeoPoint

EARTH_RADIUS_M = 6_371_008.8
LTE_TA_STEP_M = 78.12


def distance(a: GeoPoint, b: GeoPoint) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = math.radians(b.lat - a.lat), math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing(a: GeoPoint, b: GeoPoint) -> float:
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dl = math.radians(b.lon - a.lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg: float) -> str:
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[round((deg % 360) / 22.5) % 16]


def destination(origin: GeoPoint, bearing_deg: float, dist_m: float) -> GeoPoint:
    d = dist_m / EARTH_RADIUS_M
    t = math.radians(bearing_deg)
    p1, l1 = math.radians(origin.lat), math.radians(origin.lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(t))
    l2 = l1 + math.atan2(math.sin(t) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return GeoPoint(math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180)


def ta_distance(ta: int, family: Family, scs_khz: int = 30) -> tuple[float, float] | None:
    if ta < 0:
        return None
    if family is Family.LTE:
        step = LTE_TA_STEP_M
    elif family is Family.NR:
        step = LTE_TA_STEP_M * 15 / max(scs_khz, 15)
    elif family is Family.GSM:
        step = 553.85
    else:
        return None
    return ta * step, (ta + 1) * step


def weighted_centroid(samples: Iterable[tuple[GeoPoint, float | None]]) -> GeoPoint | None:
    """Power-weighted centroid: stronger readings pull the estimate towards themselves."""
    valid = [(p, dbm) for p, dbm in samples if p.valid]
    if not valid:
        return None
    strongest = max((d for _, d in valid if d is not None), default=0.0)
    sx = sy = sz = sw = 0.0
    for p, dbm in valid:
        w = 10 ** ((dbm - strongest) / 10) if dbm is not None else 0.01
        phi, lam = math.radians(p.lat), math.radians(p.lon)
        sx += w * math.cos(phi) * math.cos(lam)
        sy += w * math.cos(phi) * math.sin(lam)
        sz += w * math.sin(phi)
        sw += w
    if sw == 0:
        return None
    sx, sy, sz = sx / sw, sy / sw, sz / sw
    return GeoPoint(math.degrees(math.atan2(sz, math.hypot(sx, sy))), math.degrees(math.atan2(sy, sx)))
