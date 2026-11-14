from datetime import datetime, timedelta, timezone

import pytest

from cellscan import analysis, export, geo, identity, operators, signal, spectrum
from cellscan.models import CellKey, Family, GeoPoint, Observation, Radio, format_plmn, parse_plmn

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
HERE = GeoPoint(50.08, 14.42, 5)


def obs(dt, radio=Radio.LTE, **kw):
    kw.setdefault("mcc", 230)
    kw.setdefault("mnc", 3)
    kw.setdefault("lat", HERE.lat)
    kw.setdefault("lon", HERE.lon)
    return Observation(radio=radio, timestamp=T0 + timedelta(seconds=dt), source=kw.pop("source", "modem"), **kw)


def test_radio_parse():
    assert Radio.parse("LTE") is Radio.LTE
    assert Radio.parse("NR5G-NSA") is Radio.NR_NSA
    assert Radio.parse("5GNR") is Radio.NR_SA
    assert Radio.parse("HSPA+") is Radio.HSPA
    assert Radio.parse("weird") is Radio.UNKNOWN
    assert Radio.NR_NSA.family is Family.NR and Radio.EDGE.generation == 2


def test_plmn():
    assert format_plmn(310, 260) == "310-260"
    assert format_plmn(230, 3) == "230-03"
    assert format_plmn(310, 4) == "310-004"
    assert parse_plmn("23003") == (230, 3, 2)
    assert parse_plmn("310-260") == (310, 260, 3)
    assert parse_plmn("12") is None


def test_lte_channels():
    b3 = spectrum.channel_info(Family.LTE, 1300)
    assert (b3.band, b3.dl_mhz, b3.ul_mhz, b3.ul_channel, b3.band_label) == (3, 1815.0, 1720.0, 19300, "B3")
    b20 = spectrum.channel_info(Family.LTE, 6300)
    assert (b20.band, b20.dl_mhz, b20.ul_mhz) == (20, 806.0, 847.0)
    b66 = spectrum.channel_info(Family.LTE, 66786)
    assert (b66.band, b66.dl_mhz, b66.ul_channel) == (66, 2145.0, 132322)
    assert spectrum.channel_info(Family.LTE, 67300).ul_channel is None  # unpaired top of B66
    assert spectrum.channel_info(Family.LTE, 19300).channel == 1300  # uplink EARFCN entered
    assert spectrum.LTE_BANDS[20].earfcn(806.0) == 6300
    # no overlapping downlink ranges
    ranges = sorted((b.dl_offset, b.dl_high_channel) for b in spectrum.LTE_BANDS.values())
    assert all(a[1] < b[0] for a, b in zip(ranges, ranges[1:]))


def test_nr_channels():
    assert spectrum.nr_arfcn_to_mhz(632628) == pytest.approx(3489.42)
    assert spectrum.mhz_to_nr_arfcn(3489.42) == 632628
    assert spectrum.channel_info(Family.NR, 632628, mcc=230).band == 78
    assert spectrum.channel_info(Family.NR, 632628, mcc=310).band == 77
    assert spectrum.channel_info(Family.NR, 632628, mcc=310, reported_band=78).band == 78
    assert spectrum.channel_info(Family.NR, 520110, mcc=310).band == 41


def test_umts_gsm_channels():
    u = spectrum.channel_info(Family.UMTS, 10700)
    assert (u.band, u.dl_mhz, u.band_label) == (1, 2140.0, "Band I")
    assert spectrum.channel_info(Family.UMTS, 3011).dl_mhz == 942.2
    assert spectrum.gsm_channel_info(62).dl_mhz == 947.4
    assert spectrum.gsm_channel_info(600, 230).band_name == "DCS 1800"
    assert spectrum.gsm_channel_info(600, 310).band_name == "PCS 1900"
    assert spectrum.gsm_arfcn_for_dl(947.4) == 62
    assert spectrum.format_mhz(1815) == "1815 MHz"


def test_identity():
    eci = identity.compose_eci(104_837, 21)
    d = identity.decode(Family.LTE, eci, 310, 260)
    assert (d.site_id, d.local_id, d.global_id, d.hex) == (104_837, 21, "310-260-104837-21", "0x1998515")
    nci = (0x123456 << 12) | 0x789
    assert identity.decode(Family.NR, nci).site_id == 0x123456
    assert identity.decode(Family.NR, nci, gnb_bits=22).local_id == nci & 0x3FFF
    assert identity.decode(Family.UMTS, (0xA3 << 16) | 0x1C2D).site_id == 0xA3
    assert identity.pci_parts(377, Family.NR) == (125, 2)


def test_operators():
    assert operators.describe(230, 3) == "Vodafone CZ (Czechia)"
    assert operators.describe(262, 99) == "262-99 (Germany)"
    assert operators.flag("CZ") == "🇨🇿"
    assert not operators.is_known_mcc(123)


def test_signal():
    assert signal.grade("rsrp", -75) is signal.Grade.EXCELLENT
    assert signal.grade("rsrp", -105) is signal.Grade.POOR
    assert signal.grade("sinr", -2) is signal.Grade.POOR
    assert signal.score(-60, -3, 30) == 100 and signal.score(-100, None, None) == 50
    assert signal.overall(Observation(rsrp=-85, sinr=2)) is signal.Grade.FAIR
    assert signal.throughput_mbps(20, 20) > 100
    assert signal.bar(-100, "rsrp", 10) == "█████░░░░░"


def test_geo():
    north = GeoPoint(51, 14.42)
    assert geo.distance(GeoPoint(50, 14.42), north) == pytest.approx(111_195, abs=100)
    assert geo.compass(geo.bearing(GeoPoint(50, 14.42), north)) == "N"
    d = geo.destination(HERE, 90, 10_000)
    assert geo.distance(HERE, d) == pytest.approx(10_000, abs=1)
    assert geo.ta_distance(10, Family.LTE)[0] == pytest.approx(781.2)
    c = geo.weighted_centroid([(GeoPoint(50, 14), -70), (GeoPoint(50.01, 14), -100)])
    assert geo.distance(c, GeoPoint(50, 14)) < 10


def test_observation_roundtrip():
    o = obs(0, cell_id=1, pci=2, channel=1300, rsrp=-90.5)
    back = Observation.from_dict(o.to_dict())
    assert back == o
    assert o.key == CellKey("lte", 230, 3, 1)


def test_aggregation_and_summary():
    cap = "c1"
    lst = [
        obs(0, cell_id=1000, pci=10, channel=1300, area_code=7, rsrp=-90),
        obs(10, pci=10, channel=1300, rsrp=-80, serving=False),
        obs(20, cell_id=1000, pci=10, channel=1300, mcc=None, mnc=None),
        obs(30, radio=Radio.NR_NSA, pci=400, channel=632628, capture_id=cap),
        obs(30, cell_id=2000, pci=11, channel=6300, area_code=8, rsrp=-100, capture_id=cap),
        obs(90, cell_id=2000, pci=11, channel=6300, area_code=8),
    ]
    cells = analysis.aggregate(lst)
    main = next(c for c in cells if c.cell_id == 1000)
    assert len(main.observations) == 3 and main.rsrp.max == -80 and main.band_label == "B3"
    assert main.title() == "eNB 3·232"
    s = analysis.summarize(lst)
    assert s.handovers == 1 and s.cells == 3
    assert any(e.kind == "area" for e in s.events)
    assert s.bands["B3"] == 3


def test_anomalies():
    lst = [
        obs(0, radio=Radio.LTE),
        obs(30, radio=Radio.EDGE),
        obs(40, radio=Radio.GSM, cell_id=1234, mcc=1, mnc=1, serving=False, source="sdr-gsm"),
        obs(50, cell_id=5000, pci=10, channel=1300, area_code=100),
        obs(60, cell_id=5000, pci=11, channel=1300, area_code=100),
        obs(70, cell_id=6000, pci=20, channel=1300, area_code=100),
        obs(80, cell_id=7000, pci=30, channel=1300, area_code=666),
        obs(90, cell_id=6000, pci=20, channel=1300, area_code=100, rsrp=-45),
    ]
    kinds = {a.kind for a in analysis.detect_anomalies(lst)}
    assert {"test_network", "downgrade_2g", "identity_conflict", "transient_area", "strong_signal"} <= kinds
    clean = [obs(0, cell_id=1, pci=1, channel=1300, area_code=1, rsrp=-90), obs(60, cell_id=1, pci=1, channel=1300, area_code=1)]
    assert analysis.detect_anomalies(clean) == []


def test_pci_collision_and_db_checks():
    near = geo.destination(HERE, 45, 500)
    lst = [obs(0, cell_id=100, pci=7, channel=1300), obs(10, cell_id=200, pci=7, channel=1300, lat=near.lat, lon=near.lon),
           obs(20, cell_id=300, pci=8, channel=1300)]
    far = geo.destination(HERE, 0, 80_000)
    kinds = {a.kind for a in analysis.detect_anomalies(lst, towers={lst[0].key: far}, not_in_db={lst[2].key})}
    assert {"pci_collision", "tower_far", "not_in_db"} <= kinds


def test_exports():
    o = obs(0, cell_id=26_838_293, pci=301, channel=66786, area_code=12345, rsrp=-95.5, mcc=310, mnc=260, notes='a "b", c')
    csv_text = export.to_csv([o])
    lines = csv_text.strip().splitlines()
    assert lines[0].split(",") == export.CSV_COLUMNS
    assert "310,260,T-Mobile US (United States),12345,26838293,104837,21,301,66786,66,2145" in lines[1]
    assert lines[1].endswith('"a ""b"", c"')
    assert export.load(export.to_json([o])) == [o]
    jsonl = "\n".join(__import__("json").dumps(x.to_dict()) for x in [o, o])
    assert len(export.load(jsonl)) == 2
    kml = export.to_kml([o], analysis.aggregate([o]))
    assert "<styleUrl>#fair</styleUrl>" in kml and "#tower" in kml


def test_import_ios_export():
    ios = {"format": "cell-analyzer", "version": 1, "exportedAt": "2026-11-14T12:00:00.000Z", "observations": [
        {"id": "6F1C2D3E-0000-4000-8000-000000000001", "timestamp": "2026-11-14T12:00:00.000Z", "source": "fieldTestScreenshot",
         "radio": "nrNSA", "mcc": 230, "mnc": 3, "cellId": 4886718345, "pci": 455, "channel": 632628, "rsrp": -101,
         "location": {"latitude": 50.08, "longitude": 14.42, "horizontalAccuracy": 5}, "captureID": "AB"},
        {"id": "6F1C2D3E-0000-4000-8000-000000000002", "timestamp": "2026-11-14T12:00:05.000Z", "source": "system",
         "radio": "lte", "simSlot": "SIM 1"},
    ]}
    obs = export.load(__import__("json").dumps(ios))
    nr, lte = obs
    assert (nr.radio, nr.cell_id, nr.pci, nr.rsrp, nr.lat, nr.source) == (Radio.NR_NSA, 4886718345, 455, -101, 50.08, "ios-fieldTestScreenshot")
    assert lte.radio is Radio.LTE and lte.device == "SIM 1"
    assert analysis.aggregate(obs)[0].band_label == "n78"
