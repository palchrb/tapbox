#!/usr/bin/env bash
#
# TapBox test rig — install script for Raspberry Pi Zero 2 W (Raspberry Pi OS Bookworm)
#
# Installs:
#   - go-librespot : Spotify Connect daemon with a local HTTP API (this is our
#                    "librespot" — same job, but controllable via curl, which is
#                    what lets play.sh start a track from a share link)
#   - BlueZ + bluez-alsa : Bluetooth stack + ALSA bridge for A2DP headphones
#
# Then walks you through Spotify login (zeroconf): you pick the device in the
# Spotify app on your phone, credentials are persisted on the Pi.
#
# Usage:  sudo ./install.sh
# After:  sudo ./play.sh scan
#         sudo ./play.sh AA:BB:CC:DD:EE:FF "https://open.spotify.com/track/..."

set -euo pipefail

DEVICE_NAME="TapBox Test"
API_PORT=3678

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-pi}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
CONF_DIR="$RUN_HOME/.config/go-librespot"

echo "==> [1/6] Installing packages (BlueZ, bluez-alsa, ALSA, curl, jq)..."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  bluez bluez-alsa-utils libasound2-plugin-bluez alsa-utils curl jq

echo "==> [2/6] Downloading go-librespot (latest release)..."
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
echo "    installed $(/usr/local/bin/go-librespot --version 2>/dev/null || echo /usr/local/bin/go-librespot)"

echo "==> [3/6] Writing ALSA + go-librespot config..."
# Placeholder ALSA device: play.sh rewrites this with your headset's MAC.
# Until then audio goes to a null sink, so login/playback tests don't crash.
if [[ ! -e /etc/asound.conf ]] || ! grep -q "bluealsa" /etc/asound.conf; then
  cat > /etc/asound.conf <<'EOF'
# Managed by tapbox pi/play.sh — replaced with a bluealsa device on first connect
pcm.tapbox_bt {
    type plug
    slave.pcm "null"
}
EOF
fi

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

echo "==> [4/6] Enabling services (bluetooth, bluealsa, go-librespot)..."
usermod -aG audio,bluetooth "$RUN_USER" || true
rfkill unblock bluetooth 2>/dev/null || true
systemctl enable --now bluetooth.service
# Debian bookworm ships the daemon as bluealsa.service; newer releases as bluealsad.service
systemctl enable --now bluealsa.service 2>/dev/null \
  || systemctl enable --now bluealsad.service

cat > /etc/systemd/system/go-librespot.service <<EOF
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
systemctl daemon-reload
systemctl enable --now go-librespot.service

echo "==> [5/6] Waiting for the API to come up..."
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$API_PORT/status" >/dev/null && break
  sleep 1
done

if grep -q '"username"' "$CONF_DIR/state.json" 2>/dev/null; then
  echo "==> [6/6] Already logged in to Spotify — done!"
  exit 0
fi

echo "==> [6/6] Spotify login"
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
    echo "    Next: sudo ./play.sh scan   (with your headset in pairing mode)"
    exit 0
  fi
  sleep 5
done

echo
echo "    Timed out waiting, but the daemon keeps running — you can connect from"
echo "    the phone at any time. Check status with: journalctl -u go-librespot -f"
