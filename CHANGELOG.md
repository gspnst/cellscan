# Changelog

## 0.1.2

- Fix: `lte-scan --guided` and `sweep --band` found no carriers when LTE filled the whole band (e.g. B20 in
  Europe), because the noise floor was estimated on the carriers themselves. Band sweeps now include a
  margin of spectrum on both sides, and the floor uses the 10th percentile.
- Adjacent carriers that merge into one wide block are split at power dips; gap-less blocks are tiled with
  every fitting LTE width so the guided scan still tries the right centres.
- When the guided sweep finds nothing it prints the spectrum and min/median/max power to help diagnose
  antenna or gain problems.

## 0.1.1

- `lte-scan --guided`: sweep the band first and search only the carriers found (seconds instead of minutes).
- `--max-cfo` for `lte-scan` and `monitor`: wider frequency-offset search for RTL-SDR dongles without TCXO.
- SDR-only dashboard (`monitor --source none`) explains how to sweep and scan instead of waiting for a modem.
- `install-kali.sh` run from a clone installs in editable mode, so `git pull` updates the `cellscan` command.

## 0.1.0

First release.

- Laptop WWAN modem sources: ModemManager (`mmcli -J`), Qualcomm QMI (`qmicli --nas-get-cell-location-info`),
  Quectel `AT+QENG` (via ModemManager or a serial AT port).
- gpsd position, or a fixed position.
- SDR: native LTE PSS/SSS cell search (NumPy), power sweeps with carrier detection (rtl_power, hackrf_sweep,
  soapy_power), IQ capture via SoapySDR / rtl_sdr / hackrf_transfer / files; wrappers for kalibrate-rtl,
  gr-gsm `grgsm_scanner`, srsRAN `cell_search` and LTE-Cell-Scanner.
- Textual dashboard (live, cells, events & findings, spectrum) and scriptable subcommands.
- Analysis: LTE/NR/UMTS/GSM band tables, ECI/NCI decoding, signal grading, handovers, time on technology,
  signal-weighted site estimates, fake-base-station heuristics, OpenCellID lookups.
- Sessions as JSON Lines; CSV/JSON/KML export; import of iOS Cell Analyzer exports.
