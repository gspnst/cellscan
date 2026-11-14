"""IQ capture backends (receive only): SoapySDR, rtl_sdr CLI, hackrf_transfer CLI, IQ files."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np


class CaptureError(RuntimeError):
    pass


class Backend(Protocol):
    name: str

    def capture(self, freq_hz: float, rate: float, n: int, gain: float | None) -> np.ndarray:
        """Returns n complex64 samples at `rate`, tuned to freq_hz."""
        ...


# --------------------------------------------------------------------------- resampling


def lowpass_taps(cutoff: float, ntaps: int = 129) -> np.ndarray:
    """Windowed-sinc low-pass; cutoff as a fraction of the sample rate (0..0.5)."""
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(ntaps)
    return (h / h.sum()).astype(np.float32)


def resample(x: np.ndarray, up: int, down: int) -> np.ndarray:
    """Rational resampling by up/down with an anti-alias filter (sufficient for sync detection)."""
    if up == down:
        return x
    g = np.gcd(up, down)
    up, down = up // g, down // g
    if up > 1:
        y = np.zeros(len(x) * up, dtype=np.complex64)
        y[::up] = x
    else:
        y = x
    taps = lowpass_taps(0.5 / max(up, down) * 0.9) * up
    y = np.convolve(y, taps, mode="same")
    return y[::down].astype(np.complex64)


def to_rate(x: np.ndarray, from_rate: float, to_rate: float) -> np.ndarray:
    from fractions import Fraction

    frac = Fraction(to_rate / from_rate).limit_denominator(1000)
    return resample(x, frac.numerator, frac.denominator)


# --------------------------------------------------------------------------- sample formats


def from_cu8(raw: bytes) -> np.ndarray:
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    a = (a - 127.5) / 127.5
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def from_cs8(raw: bytes) -> np.ndarray:
    a = np.frombuffer(raw, dtype=np.int8).astype(np.float32) / 128.0
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def from_cs16(raw: bytes) -> np.ndarray:
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return (a[0::2] + 1j * a[1::2]).astype(np.complex64)


def from_cf32(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype=np.complex64).copy()


FORMATS = {"cu8": from_cu8, "cs8": from_cs8, "cs16": from_cs16, "cf32": from_cf32, "cfile": from_cf32, "fc32": from_cf32}


# --------------------------------------------------------------------------- backends


@dataclass
class RtlSdrCli:
    """`rtl_sdr` from rtl-sdr (Kali: apt install rtl-sdr)."""

    device: int = 0
    ppm: int = 0
    name: str = "rtl_sdr"

    def capture(self, freq_hz: float, rate: float, n: int, gain: float | None) -> np.ndarray:
        if shutil.which("rtl_sdr") is None:
            raise CaptureError("rtl_sdr not found (apt install rtl-sdr)")
        hw_rate = rate if rate >= 900_000 else 2_048_000
        settle = int(hw_rate * 0.05)  # drop the first 50 ms after tuning
        count = int((n * hw_rate / rate) + settle)
        cmd = ["rtl_sdr", "-d", str(self.device), "-f", str(int(freq_hz)), "-s", str(int(hw_rate)),
               "-n", str(count), "-p", str(self.ppm)]
        if gain is not None:
            cmd += ["-g", str(gain)]
        cmd.append("-")
        proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        if proc.returncode != 0 or len(proc.stdout) < 2 * count * 0.9:
            raise CaptureError(f"rtl_sdr failed: {proc.stderr.decode(errors='replace').strip()[-300:]}")
        x = from_cu8(proc.stdout)[settle:]
        return to_rate(x, hw_rate, rate)[:n]


@dataclass
class HackRFCli:
    """`hackrf_transfer` (Kali: apt install hackrf). HackRF needs >= 2 Msps, so we capture 3.84 Msps and decimate."""

    serial: str | None = None
    lna_gain: int = 32
    name: str = "hackrf"

    def capture(self, freq_hz: float, rate: float, n: int, gain: float | None) -> np.ndarray:
        if shutil.which("hackrf_transfer") is None:
            raise CaptureError("hackrf_transfer not found (apt install hackrf)")
        hw_rate = rate * max(2, int(np.ceil(2_000_000 / rate)))
        settle = int(hw_rate * 0.05)
        count = int(n * hw_rate / rate) + settle
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "capture.cs8"
            cmd = ["hackrf_transfer", "-r", str(out), "-f", str(int(freq_hz)), "-s", str(int(hw_rate)),
                   "-n", str(count), "-l", str(self.lna_gain), "-g", str(int(gain if gain is not None else 30)), "-a", "0"]
            if self.serial:
                cmd += ["-d", self.serial]
            proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
            if proc.returncode != 0 or not out.exists():
                raise CaptureError(f"hackrf_transfer failed: {proc.stderr.decode(errors='replace').strip()[-300:]}")
            x = from_cs8(out.read_bytes())[settle:]
        return to_rate(x, hw_rate, rate)[:n]


@dataclass
class SoapyBackend:
    """Any SoapySDR device (RTL-SDR, HackRF, BladeRF, LimeSDR, USRP, Airspy…). Needs python3-soapysdr."""

    args: str = ""
    name: str = "soapy"

    def capture(self, freq_hz: float, rate: float, n: int, gain: float | None) -> np.ndarray:
        try:
            import SoapySDR  # type: ignore[import-not-found]
            from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CaptureError("SoapySDR Python bindings missing (apt install python3-soapysdr)") from exc
        dev = SoapySDR.Device(self.args)
        rates = dev.listSampleRates(SOAPY_SDR_RX, 0) or [rate]
        hw_rate = rate if any(abs(r - rate) < 1 for r in rates) else min((r for r in rates if r >= rate), default=rates[-1])
        dev.setSampleRate(SOAPY_SDR_RX, 0, hw_rate)
        dev.setFrequency(SOAPY_SDR_RX, 0, freq_hz)
        if gain is None:
            dev.setGainMode(SOAPY_SDR_RX, 0, True)
        else:
            dev.setGain(SOAPY_SDR_RX, 0, gain)
        stream = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        dev.activateStream(stream)
        try:
            settle = int(hw_rate * 0.05)
            total = int(n * hw_rate / rate) + settle
            buf = np.zeros(total, dtype=np.complex64)
            got = 0
            chunk = np.zeros(65536, dtype=np.complex64)
            while got < total:
                sr = dev.readStream(stream, [chunk], min(len(chunk), total - got), timeoutUs=1_000_000)
                if sr.ret <= 0:
                    raise CaptureError(f"SoapySDR readStream error {sr.ret}")
                buf[got : got + sr.ret] = chunk[: sr.ret]
                got += sr.ret
        finally:
            dev.deactivateStream(stream)
            dev.closeStream(stream)
        return to_rate(buf[settle:], hw_rate, rate)[:n]


@dataclass
class FileBackend:
    """Replays an IQ recording (ignores the tuning request). Format from extension: .cu8 .cs8 .cs16 .cf32/.cfile."""

    path: str
    file_rate: float = 1.92e6
    name: str = "file"

    def capture(self, freq_hz: float, rate: float, n: int, gain: float | None) -> np.ndarray:
        ext = Path(self.path).suffix.lstrip(".").lower()
        reader = FORMATS.get(ext)
        if reader is None:
            raise CaptureError(f"unknown IQ format '.{ext}' (use .cu8 .cs8 .cs16 .cf32)")
        x = reader(Path(self.path).read_bytes())
        return to_rate(x, self.file_rate, rate)[:n]


def open_backend(spec: str, ppm: int = 0, file_rate: float = 1.92e6) -> Backend:
    """'rtlsdr[:index]', 'hackrf[:serial]', 'soapy[:args]', 'file:path' or an IQ file path."""
    kind, _, arg = spec.partition(":")
    kind = kind.lower()
    if kind in ("rtl", "rtlsdr", "rtl_sdr"):
        return RtlSdrCli(int(arg or 0), ppm)
    if kind == "hackrf":
        return HackRFCli(arg or None)
    if kind == "soapy":
        return SoapyBackend(arg)
    if kind == "file":
        return FileBackend(arg, file_rate)
    if os.path.exists(spec):
        return FileBackend(spec, file_rate)
    raise CaptureError(f"unknown SDR '{spec}' (rtlsdr, hackrf, soapy:driver=…, file:path)")
