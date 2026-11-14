"""Textual dashboard: live serving cell, neighbours, cell list, findings and spectrum."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, RichLog, Sparkline, Static, TabbedContent, TabPane

from .. import export, geo, identity, signal, spectrum
from ..engine import Engine
from ..models import Observation
from ..sdr.sweep import Carrier, Spectrum

SweepFn = Callable[[], tuple[Spectrum, list[Carrier]]]
ScanFn = Callable[[list[Carrier]], list[Observation]]


def _ago(ts: datetime) -> str:
    s = int((datetime.now(timezone.utc) - ts).total_seconds())
    return f"{s}s" if s < 120 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


def _metric_line(name: str, metric: str, value: float | None, family, unit: str) -> Text:
    t = Text(f"{name:<5}")
    if value is None:
        t.append("  –", style="dim")
        return t
    g = signal.grade(metric, value, family)
    t.append(f"{value:7.1f} {unit:<3} ", style="bold")
    t.append(signal.bar(value, metric, 16), style=g.color)
    t.append(f" {g.label}", style=g.color)
    return t


def serving_panel(obs: list[Observation], gnb_bits: int, here=None) -> Group | Text:
    if not obs:
        return Text("Waiting for the serving cell…  (check `cellscan doctor` if this persists)", style="dim")
    parts = []
    for o in obs:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="cyan", no_wrap=True)
        t.add_column()
        title = Text(f" {o.radio.label} ", style="bold reverse " + {5: "magenta", 4: "blue", 3: "cyan", 2: "dark_orange"}.get(o.radio.generation, "white"))
        title.append(f"  {o.plmn or ''}  ")
        from .. import operators
        title.append(operators.describe(o.mcc, o.mnc) if o.mcc is not None else "", style="dim")
        t.add_row("", title)
        if o.cell_id is not None:
            d = identity.decode(o.family, o.cell_id, o.mcc, o.mnc, gnb_bits)
            site = f"{d.site_label} {d.site_id} · cell {d.local_id}" if d.site_id is not None else f"CI {d.raw}"
            t.add_row("Cell", f"{site}   ({d.raw}, {d.hex})")
        if o.area_code is not None:
            t.add_row("TAC/LAC", f"{o.area_code} (0x{o.area_code:X})")
        if o.pci is not None:
            t.add_row("PCI", str(o.pci))
        if o.channel is not None:
            info = spectrum.channel_info(o.family, o.channel, o.mcc, o.band)
            if info:
                freq = spectrum.format_mhz(info.dl_mhz) if info.dl_mhz else "?"
                t.add_row("Channel", f"{o.channel}  {info.band_label or ''} {info.band_name or ''}  DL {freq}")
            else:
                t.add_row("Channel", str(o.channel))
        if o.bandwidth_mhz:
            t.add_row("Bandwidth", f"{o.bandwidth_mhz:g} MHz")
        if o.timing_advance is not None:
            rng = geo.ta_distance(o.timing_advance, o.family)
            if rng:
                t.add_row("Distance", f"TA {o.timing_advance} ≈ {rng[0] / 1000:.2f}–{rng[1] / 1000:.2f} km")
        sig = Group(_metric_line("RSRP", "rsrp", o.rsrp, o.family, "dBm"), _metric_line("RSRQ", "rsrq", o.rsrq, o.family, "dB"),
                    _metric_line("SINR", "sinr", o.sinr, o.family, "dB"), _metric_line("RSSI", "rssi", o.rssi, o.family, "dBm"))
        parts += [t, sig, Text("")]
    return Group(*parts)


def spectrum_text(spec: Spectrum, carriers: list[Carrier], width: int = 100, height: int = 12) -> Group:
    import numpy as np

    p = spec.power_db
    cols = np.array_split(np.arange(len(p)), min(width, len(p)))
    col_pow = np.array([p[c].max() for c in cols if len(c)])
    lo, hi = float(np.percentile(col_pow, 5)), float(col_pow.max())
    levels = np.clip((col_pow - lo) / max(hi - lo, 1e-6), 0, 1)
    blocks = " ▁▂▃▄▅▆▇█"
    rows = []
    for r in range(height, 0, -1):
        line = Text()
        for v in levels:
            fill = v * height - (r - 1)  # how much of this row the column covers (0..1)
            ch = "█" if fill >= 1 else blocks[int(max(fill, 0) * 8)]
            line.append(ch, style="green" if v > 0.6 else "yellow" if v > 0.3 else "dodger_blue2")
        rows.append(line)
    f0, f1 = spec.freqs_hz[0] / 1e6, spec.freqs_hz[-1] / 1e6
    axis = Text(f"{f0:.1f} MHz".ljust(len(levels) - 10) + f"{f1:.1f} MHz", style="dim")
    table = Table("Center", "Width", "Type", "Band", "Channel", "SNR", box=None)
    for c in carriers:
        table.add_row(f"{c.center_mhz:.3f} MHz", f"{c.width_mhz:g} MHz", c.kind, c.band_label or "", str(c.channel or ""), f"+{c.snr_db:.0f} dB")
    return Group(*rows, axis, Text(""), table)


class CellscanApp(App):
    TITLE = "cellscan"
    CSS = """
    #live-top { height: auto; }
    #serving { width: 2fr; padding: 0 1; border: round $accent; height: auto; }
    #side { width: 1fr; height: auto; }
    #status { padding: 0 1; height: auto; }
    #history { height: 6; border: round $primary; }
    #findings-box { height: auto; border: round $warning; padding: 0 1; }
    #neighbours { height: 1fr; }
    #cell-detail { height: auto; padding: 0 1; border: round $accent; }
    #anomalies { height: auto; max-height: 14; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "toggle_record", "Record on/off"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("s", "sweep", "SDR sweep"),
        Binding("l", "lte_scan", "SDR LTE scan"),
        Binding("e", "export", "Export CSV"),
    ]

    def __init__(self, engine: Engine, interval: float = 5.0, gnb_bits: int = identity.DEFAULT_GNB_BITS,
                 sweep_fn: SweepFn | None = None, scan_fn: ScanFn | None = None, subtitle: str = ""):
        super().__init__()
        self.engine = engine
        self.interval = interval
        self.gnb_bits = gnb_bits
        self.sweep_fn = sweep_fn
        self.scan_fn = scan_fn
        self.paused = False
        self.carriers: list[Carrier] = []
        self.rsrp_history: list[float] = []
        self.sub_title = subtitle

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="live"):
            with TabPane("Live", id="live"):
                yield Static(id="status")
                with Horizontal(id="live-top"):
                    yield Static(id="serving")
                    with Vertical(id="side"):
                        yield Sparkline([], id="history")
                        yield Static(id="findings-box")
                yield DataTable(id="neighbours", zebra_stripes=True)
            with TabPane("Cells", id="cells"):
                yield DataTable(id="cells-table", cursor_type="row", zebra_stripes=True)
                yield Static(id="cell-detail")
            with TabPane("Events", id="events"):
                yield DataTable(id="anomalies", zebra_stripes=True)
                yield RichLog(id="log", markup=False, highlight=False)
            with TabPane("Spectrum", id="spectrum"):
                yield Static("Press [b]s[/b] to run an SDR power sweep, [b]l[/b] to search LTE cells on the detected carriers.",
                             id="spectrum-view")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#neighbours", DataTable).add_columns("Tech", "Channel", "Band", "PCI", "RSRP", "RSRQ", "SINR", "Power", "Source")
        self.query_one("#cells-table", DataTable).add_columns(
            "Tech", "Cell", "Band", "PCI", "TAC", "Network", "Seen", "Avg RSRP", "Grade", "Source", "Last")
        self.query_one("#anomalies", DataTable).add_columns("Severity", "Finding", "When")
        self.query_one("#history", Sparkline).border_title = "serving RSRP"
        self.query_one("#findings-box", Static).border_title = "findings"
        self.set_interval(self.interval, self.poll)
        self.poll()

    # ------------------------------------------------------------------ polling

    @work(thread=True, exclusive=True, group="poll")
    def poll(self) -> None:
        if self.paused or not self.is_running:
            return
        self.engine.tick()
        if self.is_running:
            self.call_from_thread(self._safe_render)

    def _safe_render(self) -> None:
        try:
            self.render_state()
        except NoMatches:  # app is shutting down
            pass

    def render_state(self) -> None:
        st = self.engine.state
        serving = self.engine.serving()
        lte = next((o for o in serving if o.rsrp is not None), None)
        if lte is not None:
            self.rsrp_history = (self.rsrp_history + [lte.rsrp + 140])[-120:]
            self.query_one("#history", Sparkline).data = self.rsrp_history
        here = self.engine.position.point if self.engine.position is not None else None
        if not self.engine.sources and not serving:
            hint = "SDR-only mode (no modem source).\n\n"
            hint += ("Press [b]s[/b] to sweep the band, then [b]l[/b] to search LTE cells on the carriers found.\n"
                     "Decoded cells appear in the neighbour table below and in the Cells tab."
                     if self.sweep_fn else "Start with --sdr rtlsdr|hackrf --band B20 to enable SDR scans.")
            self.query_one("#serving", Static).update(Text.from_markup(hint))
        else:
            self.query_one("#serving", Static).update(serving_panel(serving, self.gnb_bits, here))
        status = Text()
        status.append("● REC " if self.engine.recording else "○ not recording ", style="bold red" if self.engine.recording else "dim")
        status.append("PAUSED " if self.paused else "", style="bold yellow")
        s = st.summary
        if s:
            status.append(f" {s.observations} samples · {s.cells} cells · {s.handovers} handovers · 5G {s.share_5g:.0%}")
        status.append(f" · GPS {here.lat:.5f},{here.lon:.5f}" if here else " · no GPS fix", style="green" if here else "yellow")
        for err in st.errors[:2]:
            status.append(f"\n⚠ {err}", style="yellow")
        self.query_one("#status", Static).update(status)

        findings = Text()
        for a in st.anomalies[:4]:
            findings.append(f"{a.severity_label.upper():<6} ", style={"high": "bold red", "medium": "dark_orange", "low": "yellow"}.get(a.severity_label, "dim"))
            findings.append(a.title + "\n")
        self.query_one("#findings-box", Static).update(findings or Text("no suspicious patterns", style="green"))

        nt = self.query_one("#neighbours", DataTable)
        nt.clear()
        for o in self.engine.neighbours()[:30]:
            info = spectrum.channel_info(o.family, o.channel, o.mcc, o.band) if o.channel is not None else None
            nt.add_row(o.radio.label, str(o.channel or ""), (info.band_label if info else "") or "", str(o.pci if o.pci is not None else ""),
                       _fmt(o.rsrp), _fmt(o.rsrq), _fmt(o.sinr), _fmt(o.rel_power, "dBFS"), o.source)

        ct = self.query_one("#cells-table", DataTable)
        selected = ct.cursor_row
        ct.clear()
        for c in st.cells:
            g = c.grade
            ct.add_row(c.family.label, c.title(self.gnb_bits), c.band_label or "", str(c.pci if c.pci is not None else ""),
                       str(c.area_code if c.area_code is not None else ""), c.operator if c.mcc is not None else "",
                       str(len(c.observations)), _fmt(c.rsrp.mean if c.rsrp else None),
                       Text(g.label, style=g.color) if g is not None else "", ",".join(sorted(c.sources)), _ago(c.last_seen),
                       key=str(c.key))
        if st.cells:
            ct.move_cursor(row=min(selected, len(st.cells) - 1))

        at = self.query_one("#anomalies", DataTable)
        at.clear()
        for a in st.anomalies:
            at.add_row(Text(a.severity_label, style={"high": "bold red", "medium": "dark_orange"}.get(a.severity_label, "yellow")),
                       a.title, a.timestamp.astimezone().strftime("%H:%M:%S"))
        log = self.query_one("#log", RichLog)
        if s:
            log.clear()
            for e in s.events[-200:]:
                log.write(f"{e.timestamp.astimezone():%H:%M:%S}  {e.text}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "cells-table" or event.row_key is None:
            return
        cell = next((c for c in self.engine.state.cells if str(c.key) == event.row_key.value), None)
        if cell is None:
            return
        lines = [f"[b]{cell.title(self.gnb_bits)}[/b]  {cell.family.label}  {cell.operator if cell.mcc is not None else ''}"]
        d = cell.decoded(self.gnb_bits)
        if d:
            lines.append(f"{d.site_label} {d.site_id}  ·  {d.local_label} {d.local_id}  ·  {d.global_id or ''}  ({d.hex})")
        info = cell.channel_info
        if info:
            ul = f", UL {spectrum.format_mhz(info.ul_mhz)}" if info.ul_mhz and info.duplex == "FDD" else ""
            lines.append(f"{info.band_label} {info.band_name or ''} {info.duplex or ''}: DL {spectrum.format_mhz(info.dl_mhz)}{ul}")
        for name, st in (("RSRP", cell.rsrp), ("RSRQ", cell.rsrq), ("SINR", cell.sinr), ("SDR power", cell.rel_power)):
            if st:
                lines.append(f"{name}: avg {st.mean:.1f}, min {st.min:.1f}, max {st.max:.1f} ({st.count} samples)")
        if cell.estimate:
            lines.append(f"Estimated site: {cell.estimate.lat:.5f}, {cell.estimate.lon:.5f} (signal-weighted)")
        tower = self.engine.towers.get(cell.key)
        if tower:
            lines.append(f"OpenCellID: {tower.lat:.5f}, {tower.lon:.5f}")
        here = self.engine.position.point if self.engine.position is not None else None
        target = tower or cell.estimate
        if here and target:
            lines.append(f"From you: {geo.distance(here, target) / 1000:.2f} km {geo.compass(geo.bearing(here, target))}")
        try:
            self.query_one("#cell-detail", Static).update("\n".join(lines))
        except NoMatches:  # event delivered during shutdown
            pass

    # ------------------------------------------------------------------ actions

    def action_toggle_record(self) -> None:
        if self.engine.writer is None:
            self.notify("Start with --session NAME to record", severity="warning")
            return
        self.engine.recording = not self.engine.recording
        self.notify("Recording" if self.engine.recording else "Recording paused")
        self.render_state()

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        self.render_state()

    def action_export(self) -> None:
        path = Path.cwd() / f"cellscan-{datetime.now():%Y%m%d-%H%M%S}.csv"
        path.write_text(export.to_csv(self.engine.state.observations, self.gnb_bits))
        self.notify(f"Exported {len(self.engine.state.observations)} rows to {path}")

    def action_sweep(self) -> None:
        if self.sweep_fn is None:
            self.notify("No SDR configured (start with --sdr rtlsdr|hackrf|soapy:… --band B20)", severity="warning")
            return
        self.notify("Sweeping…")
        self._run_sweep()

    @work(thread=True, exclusive=True, group="sdr")
    def _run_sweep(self) -> None:
        try:
            spec, carriers = self.sweep_fn()  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Sweep failed: {exc}", severity="error")
            return
        self.carriers = carriers
        self.call_from_thread(self.query_one("#spectrum-view", Static).update, spectrum_text(spec, carriers))
        self.call_from_thread(self.notify, f"{len(carriers)} carriers found")

    def action_lte_scan(self) -> None:
        if self.scan_fn is None:
            self.notify("No SDR configured", severity="warning")
            return
        self.notify("Searching LTE cells… (this takes a while)")
        self._run_scan()

    @work(thread=True, exclusive=True, group="sdr")
    def _run_scan(self) -> None:
        try:
            found = self.scan_fn(self.carriers)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self.notify, f"LTE scan failed: {exc}", severity="error")
            return
        here = self.engine.position.point if self.engine.position is not None else None
        for o in found:
            if here and o.location is None:
                o.set_location(here)
        self.engine.ingest(found, self.engine.state.errors)
        self.call_from_thread(self._safe_render)
        self.call_from_thread(self.notify, f"LTE scan: {len(found)} cells")


def _fmt(v: float | None, unit: str = "") -> str:
    return "" if v is None else f"{v:.1f}{(' ' + unit) if unit else ''}"
