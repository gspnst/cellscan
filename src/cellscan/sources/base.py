"""Common helpers for sources (subprocess runner, value parsing)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from typing import Protocol

from ..models import Observation

Runner = Callable[[Sequence[str], float], str]


class SourceError(RuntimeError):
    pass


def run(cmd: Sequence[str], timeout: float = 10.0) -> str:
    """Runs a command and returns stdout; raises SourceError with stderr on failure."""
    if shutil.which(cmd[0]) is None:
        raise SourceError(f"{cmd[0]} not found – install it (see `cellscan doctor`)")
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SourceError(f"{cmd[0]} timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        raise SourceError(f"{' '.join(cmd)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


class Source(Protocol):
    name: str

    def poll(self) -> list[Observation]:
        """Returns the observations available right now (may be empty)."""
        ...


def num(value: object) -> float | None:
    """Parses '-95.00', '-95 dBm', "'-95.3'" ; returns None for '--', 'n/a', ''."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value)
    for unit in ("dBm", "dB", "MHz", "%"):
        text = text.replace(unit, "")
    text = text.strip().strip("'\"").strip()
    if not text or text in {"--", "-", "n/a", "N/A", "unknown"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def int_or_none(value: object, base: int = 10) -> int | None:
    if value is None:
        return None
    text = str(value).strip().strip("'\"")
    if not text or text in {"--", "-", "n/a", "FFFFFFFF", "ffffffff"}:
        return None
    try:
        if base == 16 or text.lower().startswith("0x"):
            return int(text, 16)
        return int(text)
    except ValueError:
        return None
