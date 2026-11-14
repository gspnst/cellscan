#!/usr/bin/env bash
# Installs cellscan and its optional system tools on Kali / Debian.
# Usage: ./scripts/install-kali.sh [--minimal]   (--minimal skips SDR scanners)
set -euo pipefail

PKGS=(pipx python3-venv modemmanager libqmi-utils gpsd gpsd-clients rtl-sdr hackrf soapysdr-tools python3-soapysdr)
EXTRA=(kalibrate-rtl gr-gsm soapysdr-module-all)

echo "==> apt packages"
sudo apt-get update
sudo apt-get install -y "${PKGS[@]}"
if [[ "${1:-}" != "--minimal" ]]; then
  for p in "${EXTRA[@]}"; do sudo apt-get install -y "$p" || echo "   (skipped $p – not available)"; done
fi

echo "==> cellscan (pipx, with access to the system SoapySDR bindings)"
pipx install --system-site-packages "${CELLSCAN_SOURCE:-git+https://github.com/gspnst/cellscan.git}"
pipx ensurepath

echo "==> RTL-SDR: keep the DVB-T driver from grabbing the dongle"
if lsmod | grep -q dvb_usb_rtl28xxu; then
  echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl-sdr.conf >/dev/null
  sudo modprobe -r dvb_usb_rtl28xxu || true
fi

echo "==> groups (log out and in afterwards)"
sudo usermod -aG plugdev,dialout "$USER" || true

echo "==> done. Run: cellscan doctor"
