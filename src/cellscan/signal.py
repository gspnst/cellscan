"""Signal grading and simple link-quality estimates."""

from __future__ import annotations

import math
from enum import IntEnum

from .models import Family, Observation


class Grade(IntEnum):
    VERY_POOR = 0
    POOR = 1
    FAIR = 2
    GOOD = 3
    EXCELLENT = 4

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").title()

    @property
    def color(self) -> str:
        return {4: "green", 3: "spring_green3", 2: "yellow", 1: "dark_orange", 0: "red"}[int(self)]


UNITS = {"rsrp": "dBm", "rsrq": "dB", "sinr": "dB", "rssi": "dBm"}
PLAUSIBLE = {"rsrp": (-156, -31), "rsrq": (-43, 20), "sinr": (-23, 40), "rssi": (-125, -10)}
GAUGE = {"rsrp": (-140, -60), "rsrq": (-20, -3), "sinr": (-10, 30), "rssi": (-110, -50)}


def grade(metric: str, value: float, family: Family = Family.LTE) -> Grade:
    thresholds = {
        "rsrp": (-80, -90, -100, -110),
        "rsrq": (-10, -15, -20, -25),
        "sinr": (20, 13, 0, -5),
    }.get(metric)
    if metric == "rssi":
        thresholds = {Family.GSM: (-70, -85, -100, -110), Family.UMTS: (-75, -85, -95, -105)}.get(family, (-65, -75, -85, -95))
    assert thresholds is not None, metric
    for level, t in zip((Grade.EXCELLENT, Grade.GOOD, Grade.FAIR, Grade.POOR), thresholds):
        if value >= t:
            return level
    return Grade.VERY_POOR


def normalized(metric: str, value: float) -> float:
    lo, hi = GAUGE[metric]
    return min(max((value - lo) / (hi - lo), 0.0), 1.0)


def score(rsrp: float | None, rsrq: float | None, sinr: float | None, rssi: float | None = None) -> int | None:
    total = weight = 0.0
    for metric, value, w in (("rsrp", rsrp, 0.5), ("sinr", sinr, 0.3), ("rsrq", rsrq, 0.2)):
        if value is not None:
            total += w * normalized(metric, value)
            weight += w
    if weight == 0 and rssi is not None:
        total, weight = normalized("rssi", rssi), 1.0
    return None if weight == 0 else round(100 * total / weight)


def overall(o: Observation) -> Grade | None:
    grades = []
    if o.rsrp is not None:
        grades.append(grade("rsrp", o.rsrp, o.family))
    if o.sinr is not None:
        grades.append(grade("sinr", o.sinr, o.family))
    if not grades and o.rssi is not None:
        grades.append(grade("rssi", o.rssi, o.family))
    return min(grades) if grades else None


def throughput_mbps(sinr: float, bandwidth_mhz: float, layers: int = 2) -> float:
    """Attenuated Shannon estimate; relative comparisons only."""
    eff = min(0.75 * math.log2(1 + 10 ** (sinr / 10)), 7.4)
    return eff * bandwidth_mhz * 0.9 * layers


def bar(value: float | None, metric: str, width: int = 10) -> str:
    if value is None:
        return "·" * width
    filled = round(normalized(metric, value) * width)
    return "█" * filled + "░" * (width - filled)
