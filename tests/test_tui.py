import numpy as np
from textual.widgets import DataTable, TabbedContent

from cellscan.demo import DemoSource
from cellscan.engine import Engine
from cellscan.models import Observation, Radio
from cellscan.sdr import sweep
from cellscan.tui.app import CellscanApp


def fake_sweep():
    freqs = np.arange(791e6, 821e6, 100e3) + 50e3
    p = -70 + np.random.default_rng(0).normal(0, 0.5, len(freqs))
    p[np.abs(freqs - 806e6) < 4.5e6] += 25
    spec = sweep.Spectrum(freqs, p)
    return spec, sweep.detect_carriers(spec)


def fake_scan(carriers):
    return [Observation(radio=Radio.LTE, source="sdr-lte", serving=False, channel=c.channel, pci=301, rel_power=-28.0)
            for c in carriers if c.channel]


async def test_dashboard_with_demo_data(tmp_path):
    engine = Engine([DemoSource(speed=5000)], session_path=tmp_path / "s.jsonl")
    app = CellscanApp(engine, interval=0.2, sweep_fn=fake_sweep, scan_fn=fake_scan)
    async with app.run_test(size=(140, 45)) as pilot:
        for _ in range(30):
            await pilot.pause(0.1)
            if engine.state.cells:
                break
        await pilot.pause(0.3)
        assert engine.state.observations, "no demo data polled"
        screen = app.export_screenshot()
        assert "PCI" in screen and "RSRP" in screen and "Telekom" in screen
        assert app.query_one("#cells-table", DataTable).row_count > 0

        await pilot.press("s")
        for _ in range(20):
            await pilot.pause(0.1)
            if app.carriers:
                break
        assert app.carriers and app.carriers[0].channel == 6300
        await pilot.press("l")
        for _ in range(20):
            await pilot.pause(0.1)
            if any(o.source == "sdr-lte" for o in engine.state.observations):
                break
        assert any(o.source == "sdr-lte" and o.pci == 301 for o in engine.state.observations)

        app.query_one(TabbedContent).active = "cells"
        await pilot.pause(0.2)
        app.query_one(TabbedContent).active = "events"
        await pilot.pause(0.2)
        await pilot.press("p")
        assert app.paused
        await pilot.press("r")
        assert engine.recording is False
    engine.close()
    assert (tmp_path / "s.jsonl").read_text().count("\n") > 0
