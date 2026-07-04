#!/usr/bin/env bash
#
# TapBox test rig — install script for Raspberry Pi Zero 2 W (Raspberry Pi OS Bookworm)
#
# Installs:
#   - go-librespot : Spotify Connect daemon with a local HTTP API (this is our
#                    "librespot" — same job, but controllable via curl, which is
#                    what lets play.sh start a track from a share link)
#   - BlueZ + bluez-alsa : Bluetooth stack + ALSA bridge for A2DP headphones
#   - mpv + yt-dlp : local files, NRK and internet radio playback
#   - PN532 RFID support (tapbox-rfid daemon) + tapbox-card / tapbox-power tools
#
# Then walks you through Spotify login (zeroconf): you pick the device in the
# Spotify app on your phone, credentials are persisted on the Pi.
#
# The script is idempotent: re-running skips everything already done, so it
# doubles as an updater after git pull. Use --update to force re-downloading
# go-librespot and upgrading the python libs.
#
# Usage:  sudo ./install.sh [--update]
# After:  sudo ./play.sh connect
#         sudo ./play.sh "https://open.spotify.com/track/..."

set -euo pipefail

DEVICE_NAME="TapBox Test"
API_PORT=3678
UPDATE=0
[[ ${1:-} == "--update" ]] && UPDATE=1

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-pi}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
CONF_DIR="$RUN_HOME/.config/go-librespot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- helpers -----------------------------------------------------------------

have_pkg() { dpkg -s "$1" >/dev/null 2>&1; }

# Replace <dest> with stdin only if content differs. Returns 0 when changed.
write_if_changed() {
  local dest="$1" tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  if cmp -s "$tmp" "$dest" 2>/dev/null; then
    rm -f "$tmp"
    return 1
  fi
  mkdir -p "$(dirname "$dest")"
  mv "$tmp" "$dest"
  return 0
}

# install(1) only if content differs. Returns 0 when changed.
install_if_changed() {  # <mode> <src> <dest>
  if cmp -s "$2" "$3" 2>/dev/null; then
    return 1
  fi
  install -m "$1" "$2" "$3"
  return 0
}

# --- 1. packages -------------------------------------------------------------

PKGS=(bluez bluez-alsa-utils libasound2-plugin-bluez alsa-utils curl jq
      mpv yt-dlp python3-venv python3-dev i2c-tools)
missing=()
for p in "${PKGS[@]}"; do have_pkg "$p" || missing+=("$p"); done
if ((${#missing[@]})); then
  echo "==> [1/8] Installing packages: ${missing[*]}"
  apt-get update
  # --no-install-recommends matters on a headless box: mpv otherwise drags in
  # icon themes, GTK and assorted desktop bits it never uses.
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
else
  echo "==> [1/8] Packages already installed — skipping apt"
fi

# --- 2. go-librespot ---------------------------------------------------------

if [[ -x /usr/local/bin/go-librespot && $UPDATE -eq 0 ]]; then
  echo "==> [2/8] go-librespot already installed — skipping (use --update to refresh)"
else
  echo "==> [2/8] Downloading go-librespot (latest release)..."
  case "$(uname -m)" in
    aarch64)        ASSET="go-librespot_linux_arm64.tar.gz" ;;
    armv6l|armv7l)  ASSET="go-librespot_linux_armv6_rpi.tar.gz" ;;
    x86_64)         ASSET="go-librespot_linux_x86_64.tar.gz" ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  curl -fL --retry 3 -o "$TMP/gl.tar.gz" \
    "https://github.com/devgianlu/go-librespot/releases/latest/download/$ASSET"
  tar -xzf "$TMP/gl.tar.gz" -C "$TMP"
  install -m 755 "$(find "$TMP" -type f -name go-librespot | head -n1)" /usr/local/bin/go-librespot
fi

# --- 3. configs --------------------------------------------------------------

echo "==> [3/8] ALSA + go-librespot config..."
# Placeholder ALSA device: play.sh rewrites this with your headset's MAC.
if [[ ! -e /etc/asound.conf ]] || ! grep -q "bluealsa\|tapbox_bt" /etc/asound.conf; then
  cat > /etc/asound.conf <<'EOF'
# Managed by tapbox pi/play.sh — replaced with a bluealsa device on first connect
pcm.tapbox_bt {
    type plug
    slave.pcm "null"
}
EOF
  echo "    wrote placeholder /etc/asound.conf"
fi

if [[ -f "$CONF_DIR/config.yml" ]]; then
  echo "    keeping existing $CONF_DIR/config.yml (delete it to regenerate)"
else
  mkdir -p "$CONF_DIR"
  cat > "$CONF_DIR/config.yml" <<EOF
device_name: "$DEVICE_NAME"
device_type: speaker
bitrate: 160  # 96 | 160 | 320 (kbps, Ogg Vorbis)
audio_backend: alsa
audio_device: tapbox_bt
server:
  enabled: true
  address: localhost
  port: $API_PORT
zeroconf_enabled: true
credentials:
  type: zeroconf
  zeroconf:
    persist_credentials: true
EOF
  chown -R "$RUN_USER:" "$CONF_DIR"
fi

# --- 4. bluetooth + go-librespot services ------------------------------------

echo "==> [4/8] Services (bluetooth, bluealsa, go-librespot, bt-reconnect)..."
usermod -aG audio,bluetooth "$RUN_USER" || true
rfkill unblock bluetooth 2>/dev/null || true
systemctl enable --now bluetooth.service
# Debian bookworm ships the daemon as bluealsa.service; newer releases as bluealsad.service
systemctl enable --now bluealsa.service 2>/dev/null \
  || systemctl enable --now bluealsad.service

GO_CHANGED=0
write_if_changed /etc/systemd/system/go-librespot.service <<EOF && GO_CHANGED=1
[Unit]
Description=go-librespot Spotify Connect daemon
After=network-online.target bluetooth.service
Wants=network-online.target

[Service]
User=$RUN_USER
ExecStart=/usr/local/bin/go-librespot --config_dir $CONF_DIR
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

RECON_CHANGED=0
write_if_changed /usr/local/bin/tapbox-bt-reconnect <<'EOF' && RECON_CHANGED=1
#!/usr/bin/env bash
# Reconnects the remembered BT headset (written by play.sh) whenever it is
# powered on near the box, so turning the headset on is all it takes.
# Cheap poll loop for the test rig; the product version will be D-Bus
# event-driven inside the orchestration daemon.
MAC_FILE=/etc/tapbox/bt-headset
rfkill unblock bluetooth 2>/dev/null || true
bluetoothctl power on >/dev/null 2>&1 || true
bluetoothctl pairable on >/dev/null 2>&1 || true
while true; do
  mac="$(cat "$MAC_FILE" 2>/dev/null || true)"
  if [[ -n $mac ]] && ! bluetoothctl info "$mac" 2>/dev/null | grep -q "Connected: yes"; then
    bluetoothctl connect "$mac" >/dev/null 2>&1 || true
  fi
  sleep 20
done
EOF
chmod 755 /usr/local/bin/tapbox-bt-reconnect

write_if_changed /etc/systemd/system/tapbox-bt-reconnect.service <<'EOF' && RECON_CHANGED=1
[Unit]
Description=TapBox bluetooth headset auto-reconnect
After=bluetooth.service
Wants=bluetooth.service

[Service]
ExecStart=/usr/local/bin/tapbox-bt-reconnect
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- 5. RFID + tools ---------------------------------------------------------

echo "==> [5/8] RFID reader support (PN532 over I2C) + tools..."
raspi-config nonint do_i2c 0 2>/dev/null || true
if [[ ! -x /opt/tapbox/venv/bin/python3 ]]; then
  python3 -m venv /opt/tapbox/venv
fi
if [[ $UPDATE -eq 1 ]] || ! /opt/tapbox/venv/bin/python3 -c 'import adafruit_pn532, evdev' 2>/dev/null; then
  echo "    installing python libs (this can take a few minutes on a Zero)..."
  /opt/tapbox/venv/bin/pip install --quiet --upgrade adafruit-circuitpython-pn532 evdev
else
  echo "    python libs already installed — skipping pip"
fi

RFID_CHANGED=0
install_if_changed 755 "$SCRIPT_DIR/rfid.py"   /usr/local/bin/tapbox-rfid   && RFID_CHANGED=1
install_if_changed 644 "$SCRIPT_DIR/nrk.py"    /usr/local/bin/nrk.py        && RFID_CHANGED=1
install_if_changed 755 "$SCRIPT_DIR/player.py" /usr/local/bin/tapbox-player && RFID_CHANGED=1
install_if_changed 755 "$SCRIPT_DIR/card.sh"  /usr/local/bin/tapbox-card  || true
install_if_changed 755 "$SCRIPT_DIR/power.sh" /usr/local/bin/tapbox-power || true
install_if_changed 755 "$SCRIPT_DIR/idle.py"  /usr/local/bin/tapbox-idle  || true

DAEMON_CHANGED=0
install_if_changed 755 "$SCRIPT_DIR/daemon.py" /usr/local/bin/tapbox-daemon && DAEMON_CHANGED=1
write_if_changed /etc/systemd/system/tapbox-daemon.service <<'EOF' && DAEMON_CHANGED=1
[Unit]
Description=TapBox orchestration daemon (playback state + API)
After=go-librespot.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/tapbox-daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

BTN_CHANGED=0
install_if_changed 755 "$SCRIPT_DIR/buttons.py" /usr/local/bin/tapbox-buttons && BTN_CHANGED=1
write_if_changed /etc/systemd/system/tapbox-buttons.service <<'EOF' && BTN_CHANGED=1
[Unit]
Description=TapBox media button daemon (AVRCP etc.)
After=bluetooth.service

[Service]
ExecStart=/opt/tapbox/venv/bin/python3 /usr/local/bin/tapbox-buttons
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

write_if_changed /etc/systemd/system/tapbox-rfid.service <<'EOF' && RFID_CHANGED=1
[Unit]
Description=TapBox RFID daemon
After=go-librespot.service

[Service]
ExecStart=/opt/tapbox/venv/bin/python3 /usr/local/bin/tapbox-rfid
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "==> [6/8] Enabling services (restarting only what changed)..."
systemctl daemon-reload
systemctl enable --now go-librespot.service tapbox-bt-reconnect.service \
  tapbox-rfid.service tapbox-buttons.service tapbox-daemon.service
[[ $GO_CHANGED    -eq 1 ]] && { echo "    go-librespot changed — restarting"; systemctl restart go-librespot.service; }
[[ $RECON_CHANGED -eq 1 ]] && { echo "    bt-reconnect changed — restarting"; systemctl restart tapbox-bt-reconnect.service; }
[[ $RFID_CHANGED  -eq 1 ]] && { echo "    rfid daemon changed — restarting"; systemctl restart tapbox-rfid.service; }
[[ $BTN_CHANGED   -eq 1 ]] && { echo "    button daemon changed — restarting"; systemctl restart tapbox-buttons.service; }
[[ $DAEMON_CHANGED -eq 1 ]] && { echo "    orchestration daemon changed — restarting"; systemctl restart tapbox-daemon.service; }

# --- 7. API + login ----------------------------------------------------------

echo "==> [7/8] Waiting for the API to come up..."
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$API_PORT/status" >/dev/null && break
  sleep 1
done

if grep -q '"username"' "$CONF_DIR/state.json" 2>/dev/null; then
  echo "==> [8/8] Already logged in to Spotify — done!"
  exit 0
fi

echo "==> [8/8] Spotify login"
echo
echo "    1. Open the Spotify app on your phone (same Wi-Fi as the Pi)"
echo "    2. Play any song, tap the devices icon (speaker/screen symbol)"
echo "    3. Select \"$DEVICE_NAME\""
echo
echo "    Waiting up to 5 minutes for you to connect..."
for _ in $(seq 1 60); do
  if grep -q '"username"' "$CONF_DIR/state.json" 2>/dev/null; then
    echo
    echo "    Logged in! Credentials are stored on the Pi and survive reboots."
    echo "    Next: sudo ./play.sh connect   (with your headset in pairing mode)"
    exit 0
  fi
  sleep 5
done

echo
echo "    Timed out waiting, but the daemon keeps running — you can connect from"
echo "    the phone at any time. Check status with: journalctl -u go-librespot -f"
