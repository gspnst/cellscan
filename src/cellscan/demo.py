"""Synthetic drive for trying cellscan without hardware (all values are made up)."""

from __future__ import annotations

import math
import random
import uuid
from datetime import datetime, timedelta, timezone

from . import geo, identity
from .models import GeoPoint, Observation, Radio

NOTE = "demo data"
ORIGIN = GeoPoint(52.5163, 13.3777)


def session(start: datetime | None = None, minutes: int = 40, mcc: int = 262, mnc: int = 1, seed: int = 42) -> list[Observation]:
    rng = random.Random(seed)
    start = start or datetime.now(timezone.utc) - timedelta(minutes=minutes + 5)
    duration = minutes * 60
    heading, route = 95.0, 14_000.0
    sites = []
    for i in range(6):
        along = geo.destination(ORIGIN, heading, i * 2_600 + 300)
        sites.append({"pos": geo.destination(along, heading + (90 if i % 2 == 0 else -90), 450), "enb": 100_200 + i * 37,
                      "gnb": None if i == 3 else 2_300_000 + i * 11, "earfcn": 6300 if i % 3 == 0 else 1300, "pci": 30 + i * 9})

    def position(t: float) -> GeoPoint:
        p = geo.destination(ORIGIN, heading, route * t / duration)
        return GeoPoint(p.lat, p.lon, 5.0)

    def rsrp(dist: float, earfcn: int) -> float:
        return round(-62 - 33 * math.log10(max(dist, 50) / 100) + (6 if earfcn == 6300 else 0) + rng.gauss(0, 2), 1)

    out: list[Observation] = []
    t = 0.0
    while t <= duration:
        here = position(t)
        minute = t / 60
        degraded = 24.5 <= minute < 25.2
        lte_only = 17 <= minute < 22
        ranked = sorted(sites, key=lambda s: geo.distance(s["pos"], here))
        serving = ranked[0]
        dist = geo.distance(serving["pos"], here)
        sector = int(geo.bearing(serving["pos"], here) // 120) % 3 + 1
        ts = start + timedelta(seconds=t)
        cap = uuid.uuid4().hex
        if degraded:
            out.append(Observation(radio=Radio.EDGE, source="demo", timestamp=ts, device="demo", mcc=mcc, mnc=mnc,
                                   area_code=4120, cell_id=31_000 + sector, channel=62, rssi=-88, notes=NOTE,
                                   lat=here.lat, lon=here.lon, accuracy=5))
        else:
            r = rsrp(dist, serving["earfcn"])
            sinr = max(-3.0, min(28.0, (r + 118) * 0.55 + rng.gauss(0, 2)))
            lte = Observation(radio=Radio.LTE, source="demo", timestamp=ts, device="demo", capture_id=cap,
                              mcc=mcc, mnc=mnc, area_code=41_200 + (1 if serving["enb"] > 100_300 else 0),
                              cell_id=identity.compose_eci(serving["enb"], sector), pci=serving["pci"] + sector - 1,
                              channel=serving["earfcn"], band=20 if serving["earfcn"] == 6300 else 3,
                              bandwidth_mhz=10 if serving["earfcn"] == 6300 else 20, rsrp=r,
                              rsrq=round(-8 - (5 if r < -100 else 0) + rng.gauss(0, 1), 1), sinr=round(sinr, 1),
                              timing_advance=int(dist / geo.LTE_TA_STEP_M), notes=NOTE, lat=here.lat, lon=here.lon, accuracy=5)
            out.append(lte)
            if serving["gnb"] and not lte_only:
                out.append(Observation(radio=Radio.NR_NSA, source="demo", timestamp=ts, device="demo", capture_id=cap,
                                       mcc=mcc, mnc=mnc, cell_id=(serving["gnb"] << 12) | sector,
                                       pci=300 + serving["pci"] + sector, channel=636_666, band=78, bandwidth_mhz=100,
                                       rsrp=round(r - 9 + rng.gauss(0, 2), 1), rsrq=-11.0, sinr=round(sinr - 4, 1),
                                       notes=NOTE, lat=here.lat, lon=here.lon, accuracy=5))
            else:
                lte.capture_id = None
            for nb in ranked[1:3]:
                d = geo.distance(nb["pos"], here)
                out.append(Observation(radio=Radio.LTE, source="demo", timestamp=ts, device="demo", serving=False,
                                       mcc=mcc, mnc=mnc, pci=nb["pci"], channel=nb["earfcn"], rsrp=rsrp(d, nb["earfcn"]),
                                       rsrq=-14.0, notes=NOTE, lat=here.lat, lon=here.lon, accuracy=5))
        t += 30
    return out


class DemoSource:
    """Replays the synthetic drive `speed` x faster than real time.

    Observations keep their original (past) timestamps so that time-based statistics and heuristics
    see realistic spacing, not a drive compressed into seconds.
    """

    name = "demo"

    def __init__(self, speed: float = 30.0, minutes: int = 40):
        self.speed, self.minutes = speed, minutes
        self._restart(42)

    def _restart(self, seed: int) -> None:
        self.data = session(start=datetime.now(timezone.utc) - timedelta(minutes=self.minutes + 1), minutes=self.minutes, seed=seed)
        self.started = datetime.now(timezone.utc)
        self.index = 0

    def poll(self) -> list[Observation]:
        elapsed = (datetime.now(timezone.utc) - self.started).total_seconds() * self.speed
        cutoff = self.data[0].timestamp + timedelta(seconds=elapsed)
        batch = []
        while self.index < len(self.data) and self.data[self.index].timestamp <= cutoff:
            batch.append(self.data[self.index])
            self.index += 1
        if self.index >= len(self.data):
            self._restart(random.randint(0, 10_000))
        return batch
