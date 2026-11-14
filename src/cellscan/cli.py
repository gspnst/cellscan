"""Command-line interface: `cellscan <command>`; run `cellscan -h` for an overview."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from rich.console import Console
from rich.table import Table

from . import __version__, analysis, export, identity, opencellid, spectrum, store
from .models import Family, GeoPoint, Observation

console = Console()
err = Console(stderr=True)


# --------------------------------------------------------------------------- helpers


def parse_freq(text: str) -> float:
    t = text.strip().lower().replace("hz", "")
    mult = {"k": 1e3, "m": 1e6, "g": 1e9}.get(t[-1:], 1)
    return float(t[:-1] if mult != 1 else t) * mult


def make_position(spec: str | None):
    from .sources.gps import GpsdClient, StaticPosition

    if not spec or spec == "none":
        return None
    if spec == "gpsd" or spec.startswith("gpsd:"):
        host, _, port = (spec.split(":", 1)[1] if ":" in spec else "127.0.0.1:2947").partition(":")
        return GpsdClient(host or "127.0.0.1", int(port or 2947)).start()
    lat, _, lon = spec.partition(",")
    return StaticPosition(float(lat), float(lon))


def make_sources(args) -> list:
    from .demo import DemoSource
    from .sources.modemmanager import ModemManagerSource
    from .sources.qmi import QMISource
    from .sources.quectel import QuectelSource

    if getattr(args, "demo", False):
        return [DemoSource(speed=args.demo_speed)]
    kinds = args.source.split(",")
    sources = []
    for kind in kinds:
        if kind in ("mm", "auto"):
            src = ModemManagerSource(args.modem)
            if args.setup:
                src.setup()
            sources.append(src)
        elif kind == "qmi":
            sources.append(QMISource(args.qmi_device))
        elif kind == "quectel":
            sources.append(QuectelSource(args.modem, args.at_port))
        elif kind == "none":
            continue
        else:
            raise SystemExit(f"unknown source {kind}")
    return sources


def cells_table(cells: list[analysis.Cell], gnb_bits: int, limit: int = 50) -> Table:
    wide = console.width >= 120
    t = Table(title=f"Cells ({len(cells)})", header_style="bold")
    for col in ("Tech", "Cell", "Band", "PCI", "TAC", "PLMN" if not wide else "Network", "Seen", "RSRP", "Grade"):
        t.add_column(col, no_wrap=col in ("Cell", "Tech", "Band"))
    if wide:
        t.add_column("Sources")
    for c in sorted(cells, key=lambda c: -len(c.observations))[:limit]:
        g = c.grade
        network = (c.operator if wide else analysis.format_plmn(c.mcc, c.mnc)) if c.mcc is not None and c.mnc is not None else ""
        row = [c.family.label, c.title(gnb_bits), c.band_label or "", _s(c.pci), _s(c.area_code), network,
               str(len(c.observations)), f"{c.rsrp.mean:.0f}" if c.rsrp else "",
               f"[{g.color}]{g.label}[/]" if g is not None else ""]
        if wide:
            row.append(",".join(sorted(c.sources)))
        t.add_row(*row)
    return t


def anomalies_table(anomalies: list[analysis.Anomaly]) -> Table:
    t = Table(title="Security checks", header_style="bold")
    t.add_column("Severity")
    t.add_column("Finding")
    t.add_column("When")
    colors = {"high": "bold red", "medium": "dark_orange", "low": "yellow", "info": "dim"}
    for a in anomalies:
        t.add_row(f"[{colors[a.severity_label]}]{a.severity_label}[/]", f"{a.title}\n[dim]{a.detail}[/]",
                  a.timestamp.astimezone().strftime("%Y-%m-%d %H:%M"))
    return t


def observations_table(obs: list[Observation], title: str) -> Table:
    t = Table(title=title, header_style="bold")
    for col in ("Serving", "Tech", "PLMN", "TAC", "Cell ID", "PCI", "Channel", "Band", "RSRP", "RSRQ", "SINR", "Power", "Notes"):
        t.add_column(col)
    for o in obs:
        info = spectrum.channel_info(o.family, o.channel, o.mcc, o.band) if o.channel is not None else None
        t.add_row("●" if o.serving else "", o.radio.label, o.plmn or "", _s(o.area_code), _s(o.cell_id), _s(o.pci),
                  _s(o.channel), (info.band_label if info else "") or "", _s(o.rsrp), _s(o.rsrq), _s(o.sinr),
                  f"{o.rel_power} dBFS" if o.rel_power is not None else "", o.notes or "")
    return t


def _s(v) -> str:
    return "" if v is None else str(v)


def save_observations(obs: list[Observation], name: str | None, position) -> Path:
    path = store.new_session_path(name)
    point = position.point if position is not None else None
    for o in obs:
        if o.location is None and point is not None:
            o.set_location(point)
    with store.SessionWriter(path) as w:
        w.write(obs)
    return path


# --------------------------------------------------------------------------- commands


def cmd_doctor(args) -> int:
    from .doctor import checks

    t = Table(title="cellscan doctor", header_style="bold")
    for col in ("Group", "Check", "", "Detail", "Fix"):
        t.add_column(col)
    for c in checks():
        t.add_row(c.group, c.name, "[green]✔[/]" if c.ok else "[red]✘[/]", c.detail, c.hint)
    console.print(t)
    return 0


def cmd_modem(args) -> int:
    sources = make_sources(args)
    position = make_position(args.gps)
    if position is not None and hasattr(position, "_thread"):
        time.sleep(1.5)
    obs: list[Observation] = []
    for src in sources:
        try:
            obs += src.poll()
        except Exception as exc:  # noqa: BLE001
            err.print(f"[red]{src.name}: {exc}[/]")
        for w in getattr(src, "warnings", []):
            err.print(f"[yellow]{src.name}: {w}[/]")
        status = getattr(src, "status", None)
        if status:
            console.print(f"[bold]{status.manufacturer or ''} {status.model or ''}[/] · {status.state} · "
                          f"{status.radio.label} · {status.operator_name or status.operator_code or ''} · "
                          f"signal {status.signal_quality}%")
    if args.json:
        print(json.dumps([o.to_dict() for o in obs], indent=2))
    elif obs:
        console.print(observations_table(obs, "Serving and neighbour cells"))
    if args.save and obs:
        console.print(f"saved to {save_observations(obs, args.save, position)}")
    return 0 if obs else 1


def cmd_monitor(args) -> int:
    from .engine import Engine, load_towers

    sources = make_sources(args)
    position = make_position(args.gps)
    session_path = store.new_session_path(args.session) if args.session else None
    towers, missing = load_towers(session_path) if session_path else ({}, set())
    engine = Engine(sources, position, session_path, towers)
    engine.not_in_db = missing
    sweep_fn = scan_fn = None
    if args.sdr:
        sweep_fn, scan_fn = sdr_callbacks(args)
    if args.no_tui:
        return _plain_monitor(engine, args.interval)
    from .tui.app import CellscanApp

    subtitle = ("demo" if args.demo else args.source) + (f" · recording {session_path.name}" if session_path else "")
    app = CellscanApp(engine, args.interval, args.gnb_bits, sweep_fn, scan_fn, subtitle)
    try:
        app.run()
    finally:
        engine.close()
    if session_path:
        console.print(f"Session saved: {session_path}")
    return 0


def _plain_monitor(engine, interval: float) -> int:
    try:
        while True:
            batch = engine.tick()
            for o in (x for x in batch if x.serving):
                console.print(f"{o.timestamp.astimezone():%H:%M:%S} {o.radio.label:<7} {o.plmn or '':<8} "
                              f"cell {_s(o.cell_id):<11} pci {_s(o.pci):<4} ch {_s(o.channel):<7} "
                              f"rsrp {_s(o.rsrp):<7} sinr {_s(o.sinr)}")
            for e in engine.state.errors:
                err.print(f"[yellow]{e}[/]")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        engine.close()
    return 0


def sdr_callbacks(args):
    from .sdr import capture, lte_scan, sweep

    lo, hi = sweep.band_sweep_range(args.band) if args.band else (parse_freq(args.start), parse_freq(args.stop))
    tool = args.sdr.split(":")[0]

    def do_sweep():
        spec = sweep.run_sweep(tool, lo, hi, parse_freq(args.bin), args.gain)
        return spec, sweep.detect_carriers(spec, args.threshold)

    def do_scan(carriers):
        backend = capture.open_backend(args.sdr, args.ppm)
        lte = [c for c in carriers if c.family is Family.LTE]
        earfcns = lte_scan.candidate_earfcns([c.center_mhz for c in lte], spread=3) if lte else \
            lte_scan.earfcns_for_band(int(args.band.lstrip("Bb")), step=5) if args.band and not args.band.lower().startswith("n") else []
        return lte_scan.scan(backend, earfcns, args.duration, args.gain, device=args.sdr,
                             max_cfo_hz=args.max_cfo * 1e3)

    return do_sweep, do_scan


def cmd_sweep(args) -> int:
    from .sdr import sweep
    from .tui.app import spectrum_text

    lo, hi = sweep.band_sweep_range(args.band) if args.band else (parse_freq(args.start), parse_freq(args.stop))
    if args.csv:
        spec = sweep.parse_power_csv(Path(args.csv).read_text())
    else:
        spec = sweep.run_sweep(args.sdr.split(":")[0], lo, hi, parse_freq(args.bin), args.gain)
    carriers = sweep.detect_carriers(spec, args.threshold, args.mcc)
    console.print(spectrum_text(spec, carriers, width=min(console.width - 2, 160)))
    if args.save:
        obs = [Observation(radio=_radio_for(c), source="sweep", serving=False, channel=c.channel,
                           bandwidth_mhz=c.width_mhz, rel_power=round(c.peak_db, 1),
                           notes=f"{c.kind} carrier {c.center_mhz} MHz, +{c.snr_db:.0f} dB") for c in carriers]
        console.print(f"saved to {save_observations(obs, args.save, make_position(args.gps))}")
    return 0


def _radio_for(c):
    from .models import Radio

    return {Family.GSM: Radio.GSM, Family.LTE: Radio.LTE, Family.NR: Radio.NR_SA}.get(c.family, Radio.UNKNOWN)


def cmd_lte_scan(args) -> int:
    from .sdr import capture, lte_scan, lte_sync

    if args.iq:
        backend = capture.FileBackend(args.iq, parse_freq(args.rate))
        x = backend.capture(0, lte_sync.FS, int(max(args.duration, 0.2) * lte_sync.FS), None)
        cells = lte_sync.cell_search(x, max_cfo_hz=args.max_cfo * 1e3)
        t = Table("PCI", "N_ID_1", "N_ID_2", "CFO", "SNR", "Power", "Frame start", title=f"LTE cells in {args.iq}")
        for c in cells:
            t.add_row(str(c.pci), str(c.nid1), str(c.nid2), f"{c.cfo_hz / 1e3:+.2f} kHz", f"{c.snr_db:.1f} dB",
                      f"{c.power_dbfs:.1f} dBFS", str(c.frame_start))
        console.print(t)
        return 0 if cells else 1
    if args.earfcn:
        earfcns = [int(e) for e in args.earfcn.split(",")]
    elif args.band and args.guided:
        earfcns = _guided_earfcns(args)
        if not earfcns:
            err.print("[yellow]The sweep found no LTE carriers in this band – try another band, more --gain, "
                      "or a lower --threshold.[/]")
            return 1
    elif args.band:
        earfcns = lte_scan.earfcns_for_band(int(args.band.lstrip("Bb")), args.step)
    else:
        err.print("give --band, --earfcn or --iq")
        return 2
    backend = capture.open_backend(args.sdr, args.ppm)
    on_capture = _debug_capture(args) if (args.debug or args.save_iq) else None
    with console.status("scanning…") as status:
        obs = lte_scan.scan(backend, earfcns, args.duration, args.gain, device=args.sdr, max_cfo_hz=args.max_cfo * 1e3,
                            progress=lambda i, n, f: status.update(f"[{i + 1}/{n}] {f:.1f} MHz"), on_capture=on_capture)
    if args.debug:
        console.print("[dim]How to read --debug: rms below about -40 dBFS → raise --gain; clipping above 1 % → lower "
                      "--gain; best PSS below ~0.2 on every channel → no LTE sync there (wrong frequency, too weak, or "
                      "not LTE); PSS ≥ 0.3 but SSS rejected → longer capture (--duration 0.08) or --max-cfo.[/]")
    if not obs:
        err.print("[yellow]No LTE cell decoded. Rerun with --debug to see the capture level and the best sync "
                  "candidates per channel (try --max-cfo 60 for RTL-SDRs without TCXO).[/]")
    if obs:
        console.print(sdr_lte_table(obs))
    if args.save and obs:
        console.print(f"saved to {save_observations(obs, args.save, make_position(args.gps))}")
    return 0


def sdr_lte_table(obs: list[Observation]) -> Table:
    t = Table(title=f"LTE cells decoded by the SDR ({len(obs)})", header_style="bold")
    for col in ("EARFCN", "DL MHz", "Band", "PCI", "SNR", "Power", "Detail"):
        t.add_column(col, no_wrap=col != "Detail")
    for o in obs:
        info = spectrum.channel_info(o.family, o.channel, o.mcc, o.band) if o.channel is not None else None
        t.add_row(_s(o.channel), f"{info.dl_mhz:.1f}" if info and info.dl_mhz else "", (info.band_label if info else "") or "",
                  _s(o.pci), f"{o.sinr:.1f} dB" if o.sinr is not None else "",
                  f"{o.rel_power:.1f} dBFS" if o.rel_power is not None else "", o.notes or "")
    return t


def _debug_capture(args):
    """Per-channel diagnostics for lte-scan: capture level and the best sync candidates, accepted or not."""
    from .sdr import lte_sync

    out_dir = Path(args.save_iq).expanduser() if args.save_iq else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    def hook(earfcn: int, mhz: float, x) -> None:
        if out_dir:
            path = out_dir / f"lte_{earfcn}_{mhz:.1f}MHz_1.92Msps.cf32"
            path.write_bytes(np.asarray(x, dtype=np.complex64).tobytes())
        if not args.debug:
            return
        st = lte_sync.capture_stats(x)
        line = (f"EARFCN {earfcn} {mhz:.1f} MHz: {st['samples']} samples, rms {st['rms_dbfs']:.1f} dBFS, "
                f"clip {st['clip_pct']:.2f} %, DC {st['dc_dbfs']:.1f} dBFS")
        cands = lte_sync.candidates(x, args.max_cfo * 1e3, min_pss_metric=0.12)[:3]
        if not cands:
            line += " | no PSS peak ≥ 0.12"
        for c in cands:
            verdict = "[green]accepted[/]" if (c.nid1 is not None and c.sss_margin >= 1.6 and c.snr_db >= -12) else "[yellow]rejected[/]"
            sss = (f"PCI {c.pci}, SSS margin {c.sss_margin:.2f}, SNR {c.snr_db:.1f} dB" if c.nid1 is not None
                   else "no SSS")
            line += f"\n    PSS {c.pss_metric:.2f} (N_ID_2 {c.nid2}, CFO {c.cfo_hz / 1e3:+.1f} kHz) → {sss} {verdict}"
        console.print(line)

    return hook


def _guided_earfcns(args) -> list[int]:
    """Sweeps the band (plus a margin for the noise floor) and returns EARFCNs around its LTE carriers."""
    from .sdr import lte_scan, sweep
    from .tui.app import spectrum_text

    if args.band.lower().startswith("n"):
        raise SystemExit("--guided works for LTE bands (B1…B88)")
    band = sweep.band_range_hz(args.band)
    lo, hi = sweep.band_sweep_range(args.band)
    tool = args.sdr.split(":")[0]
    with console.status(f"sweeping {lo / 1e6:.1f}–{hi / 1e6:.1f} MHz…"):
        spec = sweep.run_sweep(tool, lo, hi, 100_000, args.gain)
    carriers = [c for c in sweep.detect_carriers(spec, args.threshold, within_hz=band) if c.family is Family.LTE]
    for c in carriers:
        guess = " (layout guessed – no gaps visible)" if c.kind.endswith("?") else ""
        console.print(f"carrier {c.center_mhz:.1f} MHz ({c.width_mhz:g} MHz wide, {c.band_label} EARFCN {c.channel}, "
                      f"+{c.snr_db:.0f} dB){guess}")
    if not carriers:
        console.print(spectrum_text(spec, sweep.detect_carriers(spec, args.threshold), width=min(console.width - 2, 120)))
        p = spec.power_db
        console.print(f"[dim]sweep: {len(p)} bins, min {p.min():.1f} dB, median {float(np.median(p)):.1f} dB, "
                      f"max {p.max():.1f} dB – a flat spectrum (max − median < {args.threshold:.0f} dB) means nothing "
                      f"strong was received: check antenna, --gain 40, or another band.[/]")
    # ±3 raster steps (±300 kHz): a lightly loaded carrier often shows only its central 1.4 MHz in a sweep,
    # so its estimated centre can be a few hundred kHz off.
    return lte_scan.candidate_earfcns([c.center_mhz for c in carriers], band=int(args.band.lstrip("Bb")), spread=3)


def cmd_gsm_scan(args) -> int:
    from .sdr import external

    if args.tool == "kal":
        chans = external.kal_scan(args.band, args.gain)
        obs = external.kal_observations(chans)
    else:
        obs = external.grgsm_scan(args.band, args.gain, args.args)
    console.print(observations_table(obs, f"GSM ({args.tool}) – {len(obs)} carriers"))
    if args.save and obs:
        console.print(f"saved to {save_observations(obs, args.save, make_position(args.gps))}")
    return 0


def cmd_srsran_scan(args) -> int:
    from .sdr import external

    obs = external.srsran_cell_search(args.band, args.start_earfcn, args.end_earfcn, args.gain, args.args)
    console.print(observations_table(obs, f"srsRAN cell_search – {len(obs)} cells"))
    if args.save and obs:
        console.print(f"saved to {save_observations(obs, args.save, make_position(args.gps))}")
    return 0


def cmd_sessions(args) -> int:
    t = Table("Session", "Observations", "Size", title=str(store.sessions_dir()))
    for p in store.list_sessions():
        t.add_row(p.name, str(sum(1 for _ in p.open())), f"{p.stat().st_size / 1024:.0f} KiB")
    console.print(t)
    return 0


def _load(name: str | None) -> tuple[Path, list[Observation]]:
    path = store.resolve(name)
    return path, store.read(path)


def cmd_analyze(args) -> int:
    from .engine import load_towers

    path, obs = _load(args.session)
    towers, missing = load_towers(path)
    cells = analysis.aggregate(obs)
    s = analysis.summarize(obs, cells)
    anomalies = analysis.detect_anomalies(obs, towers, missing)
    if args.json:
        print(json.dumps({
            "session": str(path), "observations": s.observations, "cells": s.cells, "handovers": s.handovers,
            "radio_changes": s.radio_changes, "share_5g": s.share_5g, "bands": s.bands, "operators": s.operators,
            "time_by_radio": {r.value: t for r, t in s.time_by_radio.items()},
            "anomalies": [{"kind": a.kind, "severity": a.severity_label, "title": a.title, "detail": a.detail,
                           "time": a.timestamp.isoformat(), "cell": str(a.key) if a.key else None} for a in anomalies],
        }, indent=2))
        return 0
    console.rule(f"[bold]{path.name}")
    console.print(f"{s.observations} observations · {s.cells} cells ({s.serving_cells} served) · {s.handovers} handovers · "
                  f"{s.radio_changes} radio changes · 5G {s.share_5g:.0%} · {s.duration_s / 60:.0f} min")
    if s.time_by_radio:
        total = sum(s.time_by_radio.values())
        console.print("Time on technology: " + ", ".join(f"{r.label} {t / total:.0%}" for r, t in
                                                         sorted(s.time_by_radio.items(), key=lambda kv: -kv[1])))
    if s.bands:
        console.print("Bands: " + ", ".join(f"{b} ({n})" for b, n in sorted(s.bands.items(), key=lambda kv: -kv[1])))
    console.print(cells_table(cells, args.gnb_bits))
    console.print(anomalies_table(anomalies) if anomalies else "[green]No suspicious patterns found.[/]")
    return 0


def cmd_export(args) -> int:
    path, obs = _load(args.session)
    if args.format == "csv":
        text = export.to_csv(obs, args.gnb_bits)
    elif args.format == "json":
        text = export.to_json(obs)
    else:
        text = export.to_kml(obs, analysis.aggregate(obs), args.gnb_bits)
    if args.output and args.output != "-":
        Path(args.output).write_text(text)
        err.print(f"wrote {args.output}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_import(args) -> int:
    obs = export.load(Path(args.file).read_text())
    path = store.new_session_path(args.name or Path(args.file).stem)
    with store.SessionWriter(path) as w:
        w.write(obs)
    console.print(f"imported {len(obs)} observations into {path}")
    return 0


def cmd_lookup(args) -> int:
    from .engine import load_towers, save_towers

    key = args.key or os.environ.get("CELLSCAN_OPENCELLID_KEY", "")
    path, obs = _load(args.session)
    towers, missing = load_towers(path)
    cells = [c for c in analysis.aggregate(obs) if c.cell_id is not None and c.mcc is not None and c.mnc is not None
             and c.area_code is not None and c.family.opencellid_radio and c.key not in towers and c.key not in missing]
    for c in cells[: args.limit]:
        try:
            tower = opencellid.lookup(key, c.mcc, c.mnc, c.area_code, c.cell_id, c.family)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001
            err.print(f"[red]{c.key}: {exc}[/]")
            if "key" in str(exc).lower():
                break
            continue
        if tower:
            towers[c.key] = GeoPoint(tower.location.lat, tower.location.lon, tower.range_m)
            console.print(f"{c.title(args.gnb_bits)}: {tower.location.lat:.5f}, {tower.location.lon:.5f} (range {tower.range_m} m)")
        else:
            missing.add(c.key)
            console.print(f"{c.title(args.gnb_bits)}: not in database")
    save_towers(path, towers, missing)
    return 0


def cmd_decode(args) -> int:
    fam = Family(args.family)
    if args.cell_id is not None:
        d = identity.decode(fam, args.cell_id, args.mcc, args.mnc, args.gnb_bits)
        console.print(f"{fam.label} cell {d.raw} ({d.hex}): {d.site_label} {d.site_id}, {d.local_label} {d.local_id}"
                      + (f", global {d.global_id}" if d.global_id else ""))
    if args.channel is not None:
        info = spectrum.channel_info(fam, args.channel, args.mcc)
        if info:
            console.print(f"channel {args.channel}: {info.band_label} {info.band_name or ''} {info.duplex or ''}, "
                          f"DL {spectrum.format_mhz(info.dl_mhz) if info.dl_mhz else '?'}"
                          + (f", UL {spectrum.format_mhz(info.ul_mhz)}" if info.ul_mhz and info.duplex == "FDD" else "")
                          + (f" (also {', '.join('n' + str(b) for b in info.alternatives)})" if info.alternatives else ""))
        else:
            console.print(f"channel {args.channel}: not a valid {fam.label} channel")
    if args.pci is not None:
        parts = identity.pci_parts(args.pci, fam)
        console.print(f"PCI {args.pci}: N_ID_1 {parts[0]}, N_ID_2 {parts[1]}" if parts else "invalid PCI")
    return 0


def cmd_demo(args) -> int:
    from . import demo

    obs = demo.session()
    path = store.new_session_path("demo")
    with store.SessionWriter(path) as w:
        w.write(obs)
    console.print(f"Demo session written to {path} ({len(obs)} observations)")
    if args.live:
        args.demo, args.session, args.sdr = True, None, None
        return cmd_monitor(args)
    args.session = str(path)
    args.json = False
    return cmd_analyze(args)


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cellscan", description="Passive cell-tower analyzer: WWAN modem, GPS and SDR.")
    p.add_argument("--version", action="version", version=f"cellscan {__version__}")
    p.add_argument("--gnb-bits", type=int, default=identity.DEFAULT_GNB_BITS, help="NR gNB ID length (22-32)")
    sub = p.add_subparsers(dest="command", required=True)

    def modem_opts(sp):
        sp.add_argument("--source", default="mm", help="mm | qmi | quectel | none (comma-separated)")
        sp.add_argument("--modem", default="any", help="ModemManager modem index (default any)")
        sp.add_argument("--qmi-device", default="/dev/cdc-wdm0")
        sp.add_argument("--at-port", help="serial AT port for --source quectel (e.g. /dev/ttyUSB2)")
        sp.add_argument("--setup", action="store_true", help="enable 3GPP location + extended signal in ModemManager")
        sp.add_argument("--gps", default="gpsd", help="gpsd | gpsd:host:port | LAT,LON | none")
        sp.add_argument("--demo", action="store_true", help="synthetic data instead of hardware")
        sp.add_argument("--demo-speed", type=float, default=30.0)

    def sdr_opts(sp, band_required=False):
        sp.add_argument("--sdr", default="rtlsdr", help="rtlsdr[:idx] | hackrf[:serial] | soapy:<args> | file:<path>")
        sp.add_argument("--band", required=band_required, help="B20, n78, gsm900 …")
        sp.add_argument("--gain", type=float, help="tuner gain in dB (default: auto)")
        sp.add_argument("--ppm", type=int, default=0, help="frequency correction (find it with `kal`)")

    sp = sub.add_parser("doctor", help="check tools, hardware and permissions")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("modem", help="one-shot readout of serving/neighbour cells from the laptop modem")
    modem_opts(sp)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--save", metavar="NAME", help="store as a session")
    sp.set_defaults(func=cmd_modem)

    sp = sub.add_parser("monitor", help="live terminal dashboard (records with --session)")
    modem_opts(sp)
    sp.add_argument("--interval", type=float, default=5.0)
    sp.add_argument("--session", metavar="NAME", help="record to a new session")
    sp.add_argument("--no-tui", action="store_true", help="plain line output")
    sp.add_argument("--sdr", help="enable SDR sweep (s) and LTE scan (l) keys: rtlsdr | hackrf | soapy:…")
    sp.add_argument("--band", help="band for SDR actions (B20, n78…)")
    sp.add_argument("--start", default="791M")
    sp.add_argument("--stop", default="821M")
    sp.add_argument("--bin", default="100k")
    sp.add_argument("--gain", type=float)
    sp.add_argument("--ppm", type=int, default=0)
    sp.add_argument("--threshold", type=float, default=8.0)
    sp.add_argument("--duration", type=float, default=0.03)
    sp.add_argument("--max-cfo", type=float, default=20.0, help="LTE search frequency-offset range in kHz")
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("sweep", help="SDR power sweep and carrier detection")
    sdr_opts(sp)
    sp.add_argument("--start", default="791M")
    sp.add_argument("--stop", default="821M")
    sp.add_argument("--bin", default="100k")
    sp.add_argument("--threshold", type=float, default=8.0, help="dB above noise floor")
    sp.add_argument("--mcc", type=int, help="region hint for band naming (e.g. 310)")
    sp.add_argument("--csv", help="analyse an existing rtl_power/hackrf_sweep CSV instead")
    sp.add_argument("--save", metavar="NAME")
    sp.add_argument("--gps", default="none")
    sp.set_defaults(func=cmd_sweep)

    sp = sub.add_parser("lte-scan", help="native LTE cell search (PSS/SSS) with an SDR or an IQ file")
    sdr_opts(sp)
    sp.add_argument("--earfcn", help="comma-separated EARFCNs")
    sp.add_argument("--step", type=int, default=1, help="EARFCN step when scanning a whole band")
    sp.add_argument("--duration", type=float, default=0.03, help="capture length per channel (s)")
    sp.add_argument("--guided", action="store_true", help="sweep --band first and only search the carriers found (fast)")
    sp.add_argument("--threshold", type=float, default=8.0, help="carrier detection threshold for --guided (dB)")
    sp.add_argument("--max-cfo", type=float, default=20.0,
                    help="frequency-offset search range in kHz (use 50-60 for RTL-SDR dongles without TCXO)")
    sp.add_argument("--debug", action="store_true", help="print capture level and sync candidates per channel")
    sp.add_argument("--save-iq", metavar="DIR", help="save each capture as .cf32 (1.92 Msps) for offline analysis")
    sp.add_argument("--iq", help="analyse an IQ recording (.cu8 .cs8 .cs16 .cf32)")
    sp.add_argument("--rate", default="1.92M", help="sample rate of --iq")
    sp.add_argument("--save", metavar="NAME")
    sp.add_argument("--gps", default="none")
    sp.set_defaults(func=cmd_lte_scan)

    sp = sub.add_parser("gsm-scan", help="GSM scan via kalibrate (kal) or gr-gsm (grgsm_scanner)")
    sp.add_argument("--tool", choices=("kal", "grgsm"), default="grgsm")
    sp.add_argument("--band", default="GSM900", help="GSM900, DCS1800, GSM850, PCS1900")
    sp.add_argument("--gain", type=float)
    sp.add_argument("--args", help="device args for gr-gsm (e.g. rtl=0)")
    sp.add_argument("--save", metavar="NAME")
    sp.add_argument("--gps", default="none")
    sp.set_defaults(func=cmd_gsm_scan)

    sp = sub.add_parser("srsran-scan", help="LTE scan via srsRAN's cell_search (decodes MIB)")
    sp.add_argument("--band", type=int, required=True)
    sp.add_argument("--start-earfcn", type=int)
    sp.add_argument("--end-earfcn", type=int)
    sp.add_argument("--gain", type=float)
    sp.add_argument("--args", help="RF device args")
    sp.add_argument("--save", metavar="NAME")
    sp.add_argument("--gps", default="none")
    sp.set_defaults(func=cmd_srsran_scan)

    sp = sub.add_parser("sessions", help="list recorded sessions")
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("analyze", help="summary, cells and security checks for a session")
    sp.add_argument("session", nargs="?", help="session file, name prefix or 'latest'")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("export", help="export a session as CSV, JSON or KML")
    sp.add_argument("session", nargs="?")
    sp.add_argument("--format", "-f", choices=("csv", "json", "kml"), default="csv")
    sp.add_argument("--output", "-o")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("import", help="import a JSON export (e.g. from the iOS Cell Analyzer app)")
    sp.add_argument("file")
    sp.add_argument("--name")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("lookup", help="look up registered cell positions on OpenCellID")
    sp.add_argument("session", nargs="?")
    sp.add_argument("--key", help="API key (or env CELLSCAN_OPENCELLID_KEY)")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_lookup)

    sp = sub.add_parser("decode", help="decode cell IDs, channels and PCIs")
    sp.add_argument("family", choices=[f.value for f in Family if f is not Family.UNKNOWN])
    sp.add_argument("--cell-id", type=lambda s: int(s, 0))
    sp.add_argument("--channel", type=int)
    sp.add_argument("--pci", type=int)
    sp.add_argument("--mcc", type=int)
    sp.add_argument("--mnc", type=int)
    sp.set_defaults(func=cmd_decode)

    sp = sub.add_parser("demo", help="synthetic session: analysis, or --live for the dashboard")
    sp.add_argument("--live", action="store_true")
    sp.add_argument("--interval", type=float, default=1.0)
    sp.add_argument("--demo-speed", type=float, default=30.0)
    sp.add_argument("--no-tui", action="store_true")
    sp.add_argument("--gps", default="none")
    sp.set_defaults(func=cmd_demo, source="none", modem="any", setup=False, qmi_device="/dev/cdc-wdm0", at_port=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except FileNotFoundError as exc:
        err.print(f"[red]{exc}[/]")
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("CELLSCAN_DEBUG"):
            raise
        err.print(f"[red]error:[/] {exc}  (set CELLSCAN_DEBUG=1 for a traceback)")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
