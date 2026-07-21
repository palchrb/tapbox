#!/usr/bin/env bash
#
# pipod installer — DRAFT, additive on top of TapBox (../pi/install.sh).
# Does NOT modify anything in ../pi. Run the base TapBox install first.
#
#   sudo ./install-pipod.sh audio      write the pipod I2S+SPI config.txt
#                                       block (hifiberry-dac WITHOUT gpio=25,
#                                       so BCM25 stays the click wheel's Data)
#   sudo ./install-pipod.sh services   build click.c + install the wheel /
#                                       podui / holdswitch systemd units
#   sudo ./install-pipod.sh all        both, then prompt to reboot
#
# See HARDWARE.md (boot config) and SOFTWARE.md (install flow).

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

boot=/boot/firmware/config.txt
[[ -f $boot ]] || boot=/boot/config.txt

do_audio() {
  if grep -q '^# pipod audio' "$boot"; then
    echo "pipod audio block already present in $boot"
  else
    cat >> "$boot" <<'EOF'

# pipod audio: I2S DAC (PCM5102, XSMT tied high => NO gpio=25 enable line,
# so BCM25 remains the click wheel Data pin). Same overlay TapBox output.py
# expects for the "local" pcm.
dtparam=i2s=on
dtoverlay=hifiberry-dac
# pipod display: SPI for the ST7789 TFT
dtparam=spi=on
EOF
    echo "wrote pipod audio+spi block to $boot (reboot to apply)"
    echo "NOTE: leave dtparam=audio=on OFF; do NOT run 'tapbox-power hat-audio-on'"
    echo "      (it adds gpio=25=op,dh which collides with the wheel Data pin)."
  fi
}

do_services() {
  command -v pigpiod >/dev/null || { echo "installing pigpio..."; apt-get install -y pigpio; }
  python3 -c 'import smbus2' 2>/dev/null || { echo "installing smbus2..."; apt-get install -y python3-smbus2 || pip3 install smbus2; }
  echo "building click.c..."
  gcc -Wall -pthread -o "$HERE/clickwheel/click" "$HERE/clickwheel/click.c" -lpigpio -lrt
  echo "  -> $HERE/clickwheel/click"

  cat > /etc/systemd/system/pipod-wheel.service <<EOF
[Unit]
Description=pipod click wheel reader
After=multi-user.target
[Service]
ExecStart=$HERE/clickwheel/click
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/pipod-ui.service <<EOF
[Unit]
Description=pipod screen UI + wheel router
After=pipod-wheel.service tapbox-daemon.service
Wants=pipod-wheel.service
[Service]
ExecStart=/usr/bin/python3 $HERE/src/podui.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/pipod-hold.service <<EOF
[Unit]
Description=pipod Hold switch (lock + safe shutdown)
After=multi-user.target
[Service]
ExecStart=/usr/bin/python3 $HERE/src/holdswitch.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF

  cat > /etc/systemd/system/pipod-battery.service <<EOF
[Unit]
Description=pipod MAX17048 fuel-gauge reader
After=multi-user.target
[Service]
ExecStart=/usr/bin/python3 $HERE/src/battery.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now pipod-wheel pipod-ui pipod-hold pipod-battery
  echo "enabled pipod-wheel, pipod-ui, pipod-hold, pipod-battery"
  echo "note: I2C must be on for the fuel gauge (raspi-config / dtparam=i2c_arm=on)"
}

case "${1:-}" in
  audio)    do_audio ;;
  services) do_services ;;
  all)      do_audio; do_services; echo "done — reboot to apply the overlay." ;;
  *) sed -n '2,18p' "$0"; exit 1 ;;
esac
