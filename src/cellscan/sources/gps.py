"""Position from gpsd (JSON protocol on localhost:2947) or a fixed location."""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass

from ..models import GeoPoint


@dataclass
class Fix:
    point: GeoPoint
    time: float
    mode: int  # 2 = 2D, 3 = 3D
    speed: float | None = None


def parse_tpv(message: dict) -> Fix | None:
    if message.get("class") != "TPV" or message.get("mode", 0) < 2:
        return None
    lat, lon = message.get("lat"), message.get("lon")
    if lat is None or lon is None:
        return None
    errs = [message.get(k) for k in ("eph", "epx", "epy") if message.get(k) is not None]
    accuracy = float(errs[0]) if len(errs) == 1 else (max(errs) if errs else None)
    return Fix(GeoPoint(float(lat), float(lon), accuracy), time.time(), int(message["mode"]), message.get("speed"))


class GpsdClient:
    """Background reader keeping the latest fix. `fix` is None until gpsd reports one."""

    def __init__(self, host: str = "127.0.0.1", port: int = 2947, max_age: float = 10.0):
        self.host, self.port, self.max_age = host, port, max_age
        self._fix: Fix | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> GpsdClient:
        self._thread = threading.Thread(target=self._loop, name="gpsd", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def fix(self) -> Fix | None:
        f = self._fix
        if f is None or time.time() - f.time > self.max_age:
            return None
        return f

    @property
    def point(self) -> GeoPoint | None:
        f = self.fix
        return f.point if f else None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=5) as sock:
                    sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
                    sock.settimeout(5)
                    self.error = None
                    buf = b""
                    while not self._stop.is_set():
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            try:
                                fix = parse_tpv(json.loads(line))
                            except (json.JSONDecodeError, ValueError):
                                continue
                            if fix:
                                self._fix = fix
            except OSError as exc:
                self.error = f"gpsd: {exc}"
                self._stop.wait(5)


class StaticPosition:
    def __init__(self, lat: float, lon: float):
        self.point = GeoPoint(lat, lon, None)
        self.error = None

    def start(self) -> StaticPosition:
        return self

    def stop(self) -> None:
        pass
