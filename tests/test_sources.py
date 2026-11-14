import json

import pytest

from cellscan.models import Radio
from cellscan.sources import modemmanager as mm
from cellscan.sources import qmi, quectel
from cellscan.sources.base import SourceError, int_or_none, num
from cellscan.sources.gps import parse_tpv


def test_value_helpers():
    assert num("-95.00") == -95.0 and num("'-95.3' dBm") == -95.3 and num("--") is None
    assert int_or_none("3039", 16) == 0x3039 and int_or_none("0x1F") == 31 and int_or_none("--") is None


def test_access_technologies():
    assert mm.radio_from_access_technologies(["lte", "5gnr"]) is Radio.NR_NSA
    assert mm.radio_from_access_technologies(["5gnr"]) is Radio.NR_SA
    assert mm.radio_from_access_technologies(["hspa-plus"]) is Radio.HSPA
    assert mm.radio_from_access_technologies([]) is Radio.UNKNOWN


def test_parse_mmcli(fixture_text):
    status = mm.parse_modem(json.loads(fixture_text("mmcli_modem.json")))
    assert status.radio is Radio.NR_NSA and status.operator_name == "Vodafone CZ" and status.signal_quality == 67
    loc = mm.parse_location(json.loads(fixture_text("mmcli_location.json")))
    assert loc == {"mcc": 230, "mnc": 3, "mnc_digits": 2, "area_code": 12345, "cell_id": 26_838_293}
    sig = mm.parse_signal(json.loads(fixture_text("mmcli_signal.json")))
    assert sig["lte"] == {"rsrp": -95.0, "rsrq": -11.0, "sinr": 14.2, "rssi": -65.0}
    assert sig["nr"]["rsrp"] == -101.0 and "umts" not in sig


def test_parse_cell_info(fixture_text):
    cells = mm.parse_cell_info(json.loads(fixture_text("mmcli_cellinfo.json")))
    serving = [c for c in cells if c.serving]
    assert len(cells) == 3 and len(serving) == 2
    lte = serving[0]
    assert (lte.cell_id, lte.area_code, lte.pci, lte.channel, lte.rsrp, lte.timing_advance) == (26_838_293, 12345, 301, 1300, -95.0, 5)
    assert cells[1].pci == 45 and not cells[1].serving


class FakeRunner:
    def __init__(self, outputs, fail=()):
        self.outputs, self.fail, self.calls = outputs, set(fail), []

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        arg = next((a for a in cmd if a.startswith("--")), "")
        if arg in self.fail:
            raise SourceError(f"{arg} unsupported")
        return self.outputs.get(arg, "{}")


def test_modemmanager_source_nsa_without_cell_info(fixture_text):
    runner = FakeRunner({"": fixture_text("mmcli_modem.json"), "--location-get": fixture_text("mmcli_location.json"),
                         "--signal-get": fixture_text("mmcli_signal.json")}, fail={"--get-cell-info"})
    src = mm.ModemManagerSource(runner=runner)
    cells = src.poll()
    assert [c.radio for c in cells] == [Radio.LTE, Radio.NR_NSA]
    lte, nr = cells
    assert (lte.cell_id, lte.mcc, lte.rsrp, lte.sinr) == (26_838_293, 230, -95.0, 14.2)
    assert nr.rsrp == -101.0 and nr.capture_id == lte.capture_id
    assert not src.cell_info_supported and any("cell info" in w for w in src.warnings)


def test_modemmanager_source_with_cell_info(fixture_text):
    runner = FakeRunner({"": fixture_text("mmcli_modem.json"), "--location-get": fixture_text("mmcli_location.json"),
                         "--signal-get": fixture_text("mmcli_signal.json"), "--get-cell-info": fixture_text("mmcli_cellinfo.json")})
    cells = mm.ModemManagerSource(runner=runner).poll()
    nr = next(c for c in cells if c.family.value == "nr")
    assert nr.radio is Radio.NR_NSA and nr.channel == 632628
    assert all(c.device == "mm:any" for c in cells)


def test_qmicli_parser(fixture_text):
    cells = qmi.parse_cell_location(fixture_text("qmicli_cell_location.txt"))
    serving = cells[0]
    assert serving.serving and serving.radio is Radio.LTE
    assert (serving.mcc, serving.mnc, serving.area_code, serving.cell_id, serving.channel, serving.pci) == (230, 3, 12345, 26_838_293, 1300, 301)
    assert (serving.rsrp, serving.rsrq, serving.rssi, serving.timing_advance) == (-95.3, -11.2, -65.2, 5)
    nr = next(c for c in cells if c.family.value == "nr")
    assert (nr.radio, nr.pci, nr.channel, nr.sinr, nr.cell_id) == (Radio.NR_NSA, 455, 632628, 7.0, 4_886_718_345)
    neighbours = [c for c in cells if not c.serving]
    assert {(n.channel, n.pci) for n in neighbours} == {(1300, 45), (6300, 120)}
    assert all(n.mcc == 230 for n in neighbours)


def test_quectel_nsa(fixture_text):
    cells = quectel.parse_qeng(fixture_text("quectel_nsa.txt"))
    lte, nr = cells
    assert (lte.cell_id, lte.pci, lte.channel, lte.band, lte.bandwidth_mhz, lte.area_code, lte.sinr) == (26_838_293, 301, 1300, 3, 20.0, 12345, 15)
    assert (nr.radio, nr.pci, nr.rsrp, nr.sinr, nr.rsrq, nr.channel, nr.band, nr.bandwidth_mhz) == (Radio.NR_NSA, 455, -101, 7, -11, 632628, 78, 100)
    assert lte.capture_id == nr.capture_id is not None


def test_quectel_lte_sa_and_neighbours(fixture_text):
    lte = quectel.parse_qeng(fixture_text("quectel_lte.txt"))[0]
    assert (lte.mcc, lte.mnc, lte.mnc_digits, lte.band, lte.sinr, lte.capture_id) == (310, 260, 3, 66, pytest.approx(14.0), None)
    sa = quectel.parse_qeng(fixture_text("quectel_sa.txt"))[0]
    assert (sa.radio, sa.cell_id, sa.pci, sa.channel, sa.band, sa.rsrp, sa.sinr) == (Radio.NR_SA, 0x123456789, 455, 520110, 41, -92, 15)
    neighbours = quectel.parse_qeng(fixture_text("quectel_neighbour.txt"))
    assert [(n.channel, n.pci, n.rsrp, n.serving) for n in neighbours] == [(1300, 45, -108, False), (6300, 120, -101, False)]


def test_gpsd_tpv():
    fix = parse_tpv({"class": "TPV", "mode": 3, "lat": 50.1, "lon": 14.4, "eph": 4.5, "speed": 1.2})
    assert fix.point.lat == 50.1 and fix.point.accuracy == 4.5 and fix.mode == 3
    assert parse_tpv({"class": "TPV", "mode": 1}) is None
    assert parse_tpv({"class": "SKY"}) is None
