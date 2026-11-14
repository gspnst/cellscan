"""Quectel modules (EM05/EM12/EM160/RM500…) via `AT+QENG`.

Commands are sent either through ModemManager (`mmcli --command`, requires ModemManager started with
`--debug`) or directly to the AT serial port with pyserial.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from ..models import Observation, Radio
from .base import Runner, SourceError, int_or_none, num, run

LTE_BW = {0: 1.4, 1: 3.0, 2: 5.0, 3: 10.0, 4: 15.0, 5: 20.0}
NR_BW = {0: 5, 1: 10, 2: 15, 3: 20, 4: 25, 5: 30, 6: 40, 7: 50, 8: 60, 9: 70, 10: 80, 11: 90, 12: 100, 13: 200, 14: 400}


def _fields(line: str) -> list[str]:
    body = line.split(":", 1)[1] if ":" in line else line
    return [f.strip() for f in next(csv.reader(io.StringIO(body.strip())))]


def lte_sinr(raw: float | None) -> float | None:
    """Quectel LTE modules report SINR either in dB or as 0..250 (1/5 dB steps from -20 dB)."""
    if raw is None:
        return None
    return raw * 0.2 - 20 if raw > 40 else raw


def parse_qeng(text: str) -> list[Observation]:
    now = datetime.now(timezone.utc)
    out: list[Observation] = []
    capture = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("+QENG:"):
            continue
        f = _fields(line)
        if not f:
            continue
        kind = f[0].lower()
        if kind == "servingcell":
            f = f[2:]  # drop "servingcell", state
            if not f:
                continue
        elif kind.startswith("neighbourcell"):
            o = _neighbour(f, now)
            if o:
                out.append(o)
            continue
        rat = f[0].upper()
        o: Observation | None = None
        if rat == "LTE" and len(f) >= 16:
            # "LTE",is_tdd,MCC,MNC,cellID,PCID,earfcn,band,UL_bw,DL_bw,TAC,RSRP,RSRQ,RSSI,SINR,CQI,tx_power,srxlev
            o = Observation(radio=Radio.LTE, source="quectel", timestamp=now,
                            mcc=int_or_none(f[2]), mnc=int_or_none(f[3]), mnc_digits=len(f[3]) if f[3].isdigit() else None,
                            cell_id=int_or_none(f[4], 16), pci=int_or_none(f[5]), channel=int_or_none(f[6]),
                            band=int_or_none(f[7]), bandwidth_mhz=LTE_BW.get(int_or_none(f[9]) or -1),
                            area_code=int_or_none(f[10], 16), rsrp=num(f[11]), rsrq=num(f[12]), rssi=num(f[13]),
                            sinr=lte_sinr(num(f[14])))
        elif rat == "NR5G-NSA" and len(f) >= 8:
            # "NR5G-NSA",MCC,MNC,PCID,RSRP,SINR,RSRQ,ARFCN,band,NR_DL_bw,scs
            o = Observation(radio=Radio.NR_NSA, source="quectel", timestamp=now,
                            mcc=int_or_none(f[1]), mnc=int_or_none(f[2]), pci=int_or_none(f[3]), rsrp=num(f[4]),
                            sinr=num(f[5]), rsrq=num(f[6]), channel=int_or_none(f[7]),
                            band=int_or_none(f[8]) if len(f) > 8 else None,
                            bandwidth_mhz=NR_BW.get(int_or_none(f[9]) or -1) if len(f) > 9 else None)
        elif rat == "NR5G-SA" and len(f) >= 14:
            # "NR5G-SA",duplex,MCC,MNC,cellID,PCID,TAC,ARFCN,band,NR_DL_bw,RSRP,RSRQ,SINR,scs,srxlev
            o = Observation(radio=Radio.NR_SA, source="quectel", timestamp=now,
                            mcc=int_or_none(f[2]), mnc=int_or_none(f[3]), mnc_digits=len(f[3]) if f[3].isdigit() else None,
                            cell_id=int_or_none(f[4], 16), pci=int_or_none(f[5]), area_code=int_or_none(f[6], 16),
                            channel=int_or_none(f[7]), band=int_or_none(f[8]), bandwidth_mhz=NR_BW.get(int_or_none(f[9]) or -1),
                            rsrp=num(f[10]), rsrq=num(f[11]), sinr=num(f[12]))
        elif rat == "WCDMA" and len(f) >= 10:
            # "WCDMA",MCC,MNC,LAC,cellID,uarfcn,PSC,RAC,RSCP,ecio,...
            o = Observation(radio=Radio.UMTS, source="quectel", timestamp=now,
                            mcc=int_or_none(f[1]), mnc=int_or_none(f[2]), area_code=int_or_none(f[3], 16),
                            cell_id=int_or_none(f[4], 16), channel=int_or_none(f[5]), pci=int_or_none(f[6]),
                            rsrp=num(f[8]), rsrq=num(f[9]))
        elif rat == "GSM" and len(f) >= 8:
            # "GSM",MCC,MNC,LAC,cellid,bsic,arfcn,band,rxlev,...
            o = Observation(radio=Radio.GSM, source="quectel", timestamp=now,
                            mcc=int_or_none(f[1]), mnc=int_or_none(f[2]), area_code=int_or_none(f[3], 16),
                            cell_id=int_or_none(f[4], 16), pci=int_or_none(f[5]), channel=int_or_none(f[6]),
                            rssi=num(f[8]) if len(f) > 8 else None)
        if o is not None:
            if o.radio is Radio.NR_NSA or (o.radio is Radio.LTE and capture is None):
                capture = capture or o.id
                o.capture_id = capture
            out.append(o)
    # An LTE anchor alone in an NSA response keeps its capture id; drop it otherwise.
    if not any(o.radio is Radio.NR_NSA for o in out):
        for o in out:
            o.capture_id = None
    return out


def _neighbour(f: list[str], now: datetime) -> Observation | None:
    # "neighbourcell intra","LTE",earfcn,PCID,RSRQ,RSRP,RSSI,SINR,...
    if len(f) >= 6 and f[1].upper() == "LTE":
        return Observation(radio=Radio.LTE, source="quectel", timestamp=now, serving=False,
                           channel=int_or_none(f[2]), pci=int_or_none(f[3]), rsrq=num(f[4]), rsrp=num(f[5]),
                           rssi=num(f[6]) if len(f) > 6 else None)
    if len(f) >= 5 and f[1].upper() == "NR5G":
        # "neighbourcell","NR5G",arfcn,PCID,RSRP,RSRQ,SINR
        return Observation(radio=Radio.NR_NSA, source="quectel", timestamp=now, serving=False,
                           channel=int_or_none(f[2]), pci=int_or_none(f[3]), rsrp=num(f[4]),
                           rsrq=num(f[5]) if len(f) > 5 else None, sinr=num(f[6]) if len(f) > 6 else None)
    return None


class QuectelSource:
    name = "quectel"

    def __init__(self, modem: str = "any", at_port: str | None = None, runner: Runner = run):
        self.modem = modem
        self.at_port = at_port
        self.runner = runner

    def _at(self, command: str) -> str:
        if self.at_port:
            return _serial_command(self.at_port, command)
        return self.runner(["mmcli", "-m", self.modem, f"--command={command}"], 10)

    def poll(self) -> list[Observation]:
        serving = parse_qeng(self._at('AT+QENG="servingcell"'))
        try:
            neighbours = parse_qeng(self._at('AT+QENG="neighbourcell"'))
        except SourceError:
            neighbours = []
        lte = next((o for o in serving if o.radio is Radio.LTE), None)
        for n in neighbours:
            if lte is not None and n.radio is Radio.LTE:
                n.mcc, n.mnc, n.mnc_digits = lte.mcc, lte.mnc, lte.mnc_digits
        device = f"quectel:{self.at_port or self.modem}"
        for o in serving + neighbours:
            o.device = device
        return serving + neighbours


def _serial_command(port: str, command: str, timeout: float = 3.0) -> str:
    try:
        import serial  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SourceError("pyserial is required for --at-port (pip install pyserial)") from exc
    with serial.Serial(port, 115200, timeout=timeout) as ser:
        ser.reset_input_buffer()
        ser.write((command + "\r").encode())
        lines = []
        while True:
            line = ser.readline().decode(errors="replace").strip()
            if not line:
                break
            lines.append(line)
            if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
                break
    text = "\n".join(lines)
    if "ERROR" in text:
        raise SourceError(f"{command}: {text}")
    return text
