"""Regenerates docs/*.svg screenshots of the dashboard using demo data: python scripts/screenshots.py"""

import asyncio
from pathlib import Path

import numpy as np
from textual.widgets import TabbedContent

from cellscan.demo import DemoSource
from cellscan.engine import Engine
from cellscan.sdr import sweep
from cellscan.tui.app import CellscanApp

DOCS = Path(__file__).resolve().parent.parent / "docs"


def fake_sweep():
    freqs = np.arange(791e6, 862e6, 100e3) + 50e3
    rng = np.random.default_rng(3)
    p = -72 + rng.normal(0, 0.8, len(freqs))
    for center, width, level in ((796e6, 10e6, 22), (806e6, 10e6, 27), (816e6, 10e6, 18), (853e6, 0.2e6, 20)):
        p[np.abs(freqs - center) < width * 0.45] += level
    spec = sweep.Spectrum(freqs, p)
    return spec, sweep.detect_carriers(spec, mcc=262)


async def main() -> None:
    source = DemoSource(speed=600)
    engine = Engine([source])
    app = CellscanApp(engine, interval=0.5, sweep_fn=fake_sweep, subtitle="demo")
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause(6)
        DOCS.mkdir(exist_ok=True)
        (DOCS / "live.svg").write_text(app.export_screenshot(title="cellscan – live"))
        tabs = app.query_one(TabbedContent)
        tabs.active = "cells"
        await pilot.pause(0.5)
        await pilot.press("down", "down")
        await pilot.pause(0.5)
        (DOCS / "cells.svg").write_text(app.export_screenshot(title="cellscan – cells"))
        tabs.active = "events"
        await pilot.pause(0.5)
        (DOCS / "events.svg").write_text(app.export_screenshot(title="cellscan – events"))
        tabs.active = "spectrum"
        await pilot.press("s")
        await pilot.pause(1.5)
        (DOCS / "spectrum.svg").write_text(app.export_screenshot(title="cellscan – spectrum"))
    engine.close()


if __name__ == "__main__":
    asyncio.run(main())
