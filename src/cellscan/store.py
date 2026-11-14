"""Session storage: one JSON Lines file per session under $XDG_DATA_HOME/cellscan/sessions."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .models import Observation


def data_dir() -> Path:
    base = os.environ.get("CELLSCAN_HOME") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "cellscan")
    path = Path(base)
    (path / "sessions").mkdir(parents=True, exist_ok=True)
    return path


def sessions_dir() -> Path:
    return data_dir() / "sessions"


def new_session_path(name: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return sessions_dir() / f"{stamp}{'-' + name if name else ''}.jsonl"


def list_sessions() -> list[Path]:
    return sorted(sessions_dir().glob("*.jsonl"))


def latest_session() -> Path | None:
    sessions = list_sessions()
    return sessions[-1] if sessions else None


def resolve(name_or_path: str | None) -> Path:
    """Accepts a path, a session file name/prefix, or None/'latest'."""
    if not name_or_path or name_or_path == "latest":
        latest = latest_session()
        if latest is None:
            raise FileNotFoundError("no sessions recorded yet")
        return latest
    p = Path(name_or_path).expanduser()
    if p.exists():
        return p
    matches = [s for s in list_sessions() if s.name.startswith(name_or_path)]
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"session not found: {name_or_path}")


class SessionWriter:
    """Appends observations as JSON Lines; flushes every write so a crash loses nothing."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def write(self, observations: Iterable[Observation]) -> None:
        for o in observations:
            self._fh.write(json.dumps(o.to_dict(), separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> SessionWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read(path: Path) -> list[Observation]:
    from .export import load

    return load(path.read_text(encoding="utf-8"))
