"""Polling engine shared by the TUI and the plain-text monitor."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import analysis, store
from .models import CellKey, GeoPoint, Observation
from .sources.base import Source, SourceError


@dataclass
class EngineState:
    observations: list[Observation] = field(default_factory=list)
    cells: list[analysis.Cell] = field(default_factory=list)
    summary: analysis.Summary | None = None
    anomalies: list[analysis.Anomaly] = field(default_factory=list)
    latest: list[Observation] = field(default_factory=list)  # last poll's observations
    errors: list[str] = field(default_factory=list)
    polls: int = 0


class Engine:
    def __init__(self, sources: list[Source], position=None, session_path: Path | None = None,
                 towers: dict[CellKey, GeoPoint] | None = None):
        self.sources = sources
        self.position = position  # object with `.point` (GeoPoint | None) and `.error`
        self.writer = store.SessionWriter(session_path) if session_path else None
        self.session_path = session_path
        self.towers = towers or {}
        self.not_in_db: set[CellKey] = set()
        self.state = EngineState()
        self.recording = session_path is not None
        self._lock = threading.Lock()

    def tick(self) -> list[Observation]:
        """Polls all sources once (blocking) and updates derived analysis."""
        batch: list[Observation] = []
        errors: list[str] = []
        point = self.position.point if self.position is not None else None
        for src in self.sources:
            try:
                got = src.poll()
            except SourceError as exc:
                errors.append(f"{src.name}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - keep the monitor alive
                errors.append(f"{src.name}: {type(exc).__name__}: {exc}")
                continue
            for o in got:
                if o.location is None and point is not None:
                    o.set_location(point)
            batch.extend(got)
        for warn in (getattr(s, "warnings", []) for s in self.sources):
            errors.extend(w for w in warn if w not in errors)
        if self.position is not None and getattr(self.position, "error", None):
            errors.append(self.position.error)
        self.ingest(batch, errors)
        return batch

    def ingest(self, batch: list[Observation], errors: list[str] | None = None) -> None:
        with self._lock:
            if batch:
                self.state.observations.extend(batch)
                self.state.latest = batch
                if self.writer and self.recording:
                    self.writer.write(batch)
            self.state.errors = errors or []
            self.state.polls += 1
            self.refresh()

    def refresh(self) -> None:
        obs = self.state.observations
        self.state.cells = analysis.aggregate(obs)
        self.state.summary = analysis.summarize(obs, self.state.cells)
        self.state.anomalies = analysis.detect_anomalies(obs, self.towers, self.not_in_db)

    def serving(self) -> list[Observation]:
        """Newest serving cell per technology family from the latest poll (LTE anchor first, then NR)."""
        newest: dict[str, Observation] = {}
        for o in sorted((o for o in self.state.latest if o.serving), key=lambda o: o.timestamp):
            newest[o.family.value] = o
        return sorted(newest.values(), key=lambda o: o.radio.generation)

    def neighbours(self) -> list[Observation]:
        """Neighbours measured together with the newest serving cell (or all, for SDR-only batches)."""
        latest = [o for o in self.state.latest if not o.serving]
        if latest:
            newest = max(o.timestamp for o in latest)
            latest = [o for o in latest if o.source.startswith("sdr") or o.source == "sweep" or o.timestamp == newest]
        return sorted(latest, key=lambda o: -(o.rsrp if o.rsrp is not None else o.rel_power if o.rel_power is not None else -999))

    def close(self) -> None:
        if self.writer:
            self.writer.close()
        if self.position is not None:
            self.position.stop()


def towers_path(session: Path) -> Path:
    return session.with_suffix(".towers.json")


def load_towers(session: Path) -> tuple[dict[CellKey, GeoPoint], set[CellKey]]:
    path = towers_path(session)
    if not path.exists():
        return {}, set()
    data = json.loads(path.read_text())
    towers, missing = {}, set()
    for entry in data:
        key = CellKey(**entry["key"])
        if entry.get("lat") is not None:
            towers[key] = GeoPoint(entry["lat"], entry["lon"], entry.get("range"))
        else:
            missing.add(key)
    return towers, missing


def save_towers(session: Path, towers: dict[CellKey, GeoPoint], missing: set[CellKey]) -> None:
    from dataclasses import asdict

    entries = [{"key": asdict(k), "lat": p.lat, "lon": p.lon, "range": p.accuracy} for k, p in towers.items()]
    entries += [{"key": asdict(k)} for k in missing]
    towers_path(session).write_text(json.dumps(entries, indent=1))
