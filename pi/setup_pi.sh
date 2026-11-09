#!/usr/bin/env bash
# One-time Pi setup for the AFTERGLOW pod. Raspberry Pi OS Bookworm 64-bit.
# Run from the repo root:  bash pi/setup_pi.sh   (it uses sudo where needed)
# Safe to run twice. Reboot when it finishes.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="$(cd "$HERE/../code" && pwd)"
CFG=/boot/firmware/config.txt

echo "== packages"
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y mosquitto mosquitto-clients i2c-tools python3-venv python3-pip \
                    python3-picamera2 python3-lgpio hailo-all git \
                    python3-opencv v4l-utils python3-pil fonts-dejavu-core

echo "== config.txt"
if ! grep -q "AFTERGLOW pod" "$CFG"; then
  sudo cp "$CFG" "$CFG.bak-$(date +%s)"
  echo | sudo tee -a "$CFG" >/dev/null
  sudo tee -a "$CFG" < "$HERE/config.txt.append" >/dev/null
  echo "appended (backup saved next to it)"
else
  echo "already has the AFTERGLOW block; left alone"
fi

echo "== serial console off the GPIO UART, UART hardware on"
sudo raspi-config nonint do_serial_cons 1 || true
sudo raspi-config nonint do_serial_hw 0 || true
sudo usermod -aG dialout,i2c,spi,gpio,video "$USER"

echo "== Mosquitto"
sudo systemctl enable --now mosquitto

echo "== SSH (on at every boot) and the module service file (enable modules one by one later)"
sudo systemctl enable --now ssh
sudo cp "$CODE/../pi/systemd/afterglow@.service" /etc/systemd/system/
sudo systemctl daemon-reload

echo "== Python venv (sees apt's picamera2 and Hailo packages)"
python3 -m venv --system-site-packages "$CODE/.venv"
"$CODE/.venv/bin/pip" install -r "$CODE/requirements-pi.txt"

cat <<'MSG'

Done. Now:  sudo reboot
(The power bank gives 3 A, so the Pi may warn about its supply. That's expected:
don't set PSU_MAX_CURRENT. Check vcgencmd get_throttled reads 0x0 instead.)
After the reboot:
  cd code && .venv/bin/python tools/check_hw.py
MSG
