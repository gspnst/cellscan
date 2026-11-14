"""Environment checks: tools, SDR hardware, modem, GPS, permissions."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass


@dataclass
class Check:
    group: str
    name: str
    ok: bool
    detail: str
    hint: str = ""


TOOLS = [
    ("Modem", "mmcli", "modemmanager"),
    ("Modem", "qmicli", "libqmi-utils"),
    ("GPS", "gpspipe", "gpsd-clients"),
    ("SDR", "rtl_sdr", "rtl-sdr"),
    ("SDR", "rtl_power", "rtl-sdr"),
    ("SDR", "hackrf_transfer", "hackrf"),
    ("SDR", "hackrf_sweep", "hackrf"),
    ("SDR", "SoapySDRUtil", "soapysdr-tools"),
    ("SDR", "soapy_power", "pipx install soapy_power"),
    ("Scanners", "kal", "kalibrate-rtl"),
    ("Scanners", "grgsm_scanner", "gr-gsm"),
    ("Scanners", "cell_search", "srsRAN 4G (build from source: lib/examples/cell_search)"),
    ("Scanners", "CellSearch", "LTE-Cell-Scanner (build from source)"),
]

USB_IDS = {
    "0bda:2838": "RTL-SDR (RTL2832U)", "0bda:2832": "RTL-SDR (RTL2832U)", "1d50:6089": "HackRF One",
    "1d50:60a1": "Airspy", "2cf0:5246": "bladeRF", "2cf0:5250": "bladeRF 2.0", "0403:601f": "LimeSDR",
    "1d50:6108": "LimeSDR Mini", "2500:0020": "USRP B200/B210", "2500:0021": "USRP B200mini", "2500:0022": "USRP B205mini",
    "0456:b673": "ADALM-PLUTO", "1df7:3000": "SDRplay RSP1", "1df7:3020": "SDRplay RSP1A",
}


def _run(cmd: list[str], timeout: float = 5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def checks() -> list[Check]:
    out: list[Check] = []
    for group, exe, pkg in TOOLS:
        path = shutil.which(exe)
        out.append(Check(group, exe, path is not None, path or "not installed", "" if path else f"install: {pkg}"))

    try:
        import SoapySDR  # type: ignore[import-not-found]  # noqa: F401

        out.append(Check("SDR", "python3-soapysdr", True, "SoapySDR Python bindings available"))
    except ImportError:
        out.append(Check("SDR", "python3-soapysdr", False, "not importable",
                         "apt install python3-soapysdr (use a venv with --system-site-packages)"))

    lsusb = _run(["lsusb"])
    found = [name for vid_pid, name in USB_IDS.items() if vid_pid in lsusb.lower()]
    out.append(Check("SDR", "USB SDR hardware", bool(found), ", ".join(found) if found else "no known SDR on USB",
                     "" if found else "plug in an RTL-SDR / HackRF / SoapySDR device"))

    lsmod = _run(["lsmod"])
    if "dvb_usb_rtl28xxu" in lsmod:
        out.append(Check("SDR", "DVB driver", False, "dvb_usb_rtl28xxu is loaded and grabs RTL-SDR dongles",
                         "echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf && sudo modprobe -r dvb_usb_rtl28xxu"))

    if shutil.which("mmcli"):
        listing = _run(["mmcli", "-L"])
        modems = [line.strip() for line in listing.splitlines() if "/Modem/" in line]
        out.append(Check("Modem", "WWAN modem", bool(modems), modems[0] if modems else (listing.strip() or "ModemManager not running"),
                         "" if modems else "sudo systemctl enable --now ModemManager; check the modem isn't disabled in BIOS/rfkill"))
    if os.path.exists("/dev/cdc-wdm0"):
        out.append(Check("Modem", "QMI/MBIM device", True, "/dev/cdc-wdm0"))

    try:
        with socket.create_connection(("127.0.0.1", 2947), timeout=1):
            out.append(Check("GPS", "gpsd", True, "listening on 127.0.0.1:2947"))
    except OSError:
        out.append(Check("GPS", "gpsd", False, "not reachable on 127.0.0.1:2947",
                         "apt install gpsd; sudo systemctl enable --now gpsd (or use --gps LAT,LON)"))

    groups = _run(["id", "-nG"]).split()
    if groups:
        needed = [g for g in ("plugdev", "dialout") if g not in groups]
        out.append(Check("System", "user groups", not needed, " ".join(groups[:8]),
                         f"sudo usermod -aG {','.join(needed)} $USER (then log in again)" if needed else ""))
    return out
