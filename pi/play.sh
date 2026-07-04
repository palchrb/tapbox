#!/usr/bin/env bash
#
# TapBox test rig — connect a Bluetooth headset and play from Spotify.
# Requires install.sh to have been run (and Spotify login completed).
#
# Usage:
#   sudo ./play.sh scan                                  # find your headset's MAC
#   sudo ./play.sh AA:BB:CC:DD:EE:FF <spotify-link>      # pair + connect + play
#   sudo ./play.sh <spotify-link>                        # reuse remembered headset
#   sudo ./play.sh pause | resume | next | prev | stop   # playback control
#
# <spotify-link> can be a share link (https://open.spotify.com/track/...),
# a short link (https://spotify.link/...), or a spotify:track:... URI.
# Track, album, playlist and artist links all work.

set -euo pipefail

API="http://127.0.0.1:3678"
MAC_FILE="/etc/tapbox/bt-headset"
MAC_RE='^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$'

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $*" >&2
  exit 1
fi

usage() { sed -n '3,15p' "$0"; exit 1; }

scan() {
  bluetoothctl power on >/dev/null
  echo "Scanning for 15s — put your headset in pairing mode now..."
  bluetoothctl --timeout 15 scan on >/dev/null || true
  echo
  echo "Devices found (name + MAC):"
  bluetoothctl devices
  echo
  echo "Next: sudo $0 <MAC> <spotify-link>"
}

connect_headset() {
  local mac="$1"
  bluetoothctl power on >/dev/null

  if ! bluetoothctl info "$mac" 2>/dev/null | grep -q "Paired: yes"; then
    echo "==> Pairing with $mac (make sure it is in pairing mode)..."
    bluetoothctl --timeout 12 scan on >/dev/null || true
    bluetoothctl pair "$mac"
    bluetoothctl trust "$mac"
  fi

  if bluetoothctl info "$mac" | grep -q "Connected: yes"; then
    echo "==> Headset already connected."
  else
    echo "==> Connecting to $mac..."
    local ok=""
    for _ in 1 2 3; do
      if bluetoothctl connect "$mac"; then ok=1; break; fi
      sleep 3
    done
    if [[ -z $ok ]]; then
      echo "Could not connect. If pairing keeps failing, try interactively:" >&2
      echo "  bluetoothctl  ->  scan on / pair $mac / trust $mac / connect $mac" >&2
      exit 1
    fi
    sleep 2  # give bluealsa a moment to register the A2DP transport
  fi

  # Point the tapbox_bt ALSA device at this headset and remember the MAC
  if ! grep -q "$mac" /etc/asound.conf 2>/dev/null; then
    cat > /etc/asound.conf <<EOF
# Managed by tapbox pi/play.sh
pcm.tapbox_bt {
    type plug
    slave.pcm {
        type bluealsa
        device "$mac"
        profile "a2dp"
    }
}
EOF
    mkdir -p "$(dirname "$MAC_FILE")"
    echo "$mac" > "$MAC_FILE"
    echo "==> ALSA output routed to $mac, restarting go-librespot..."
    systemctl restart go-librespot
  fi
}

wait_for_api() {
  for _ in $(seq 1 30); do
    curl -sf "$API/status" >/dev/null && return 0
    sleep 1
  done
  echo "go-librespot API not reachable at $API — check: journalctl -u go-librespot -n 50" >&2
  exit 1
}

link_to_uri() {
  local link="$1"
  if [[ $link == *spotify.link/* ]]; then  # short links redirect to open.spotify.com
    link="$(curl -sL -o /dev/null -w '%{url_effective}' "$link")"
  fi
  if [[ $link =~ ^spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]+$ ]]; then
    echo "$link"
  elif [[ $link =~ open\.spotify\.com/(intl-[a-z-]+/)?(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+) ]]; then
    echo "spotify:${BASH_REMATCH[2]}:${BASH_REMATCH[3]}"
  else
    echo "Could not parse Spotify link: $link" >&2
    exit 1
  fi
}

show_status() {
  sleep 2
  curl -sf "$API/status" | jq -r \
    'if .track then "Now playing: \(.track.name) — \(.track.artist_names // [] | join(", "))" else "No track loaded yet — check journalctl -u go-librespot" end' \
    2>/dev/null || true
}

[[ $# -ge 1 ]] || usage

case "$1" in
  scan)
    scan; exit 0 ;;
  pause|resume|next|prev|stop)
    wait_for_api
    curl -sf -X POST "$API/player/$1" >/dev/null && echo "OK: $1"
    exit 0 ;;
esac

# Sort out MAC vs link arguments
if [[ $1 =~ $MAC_RE ]]; then
  MAC="$1"
  LINK="${2:-}"
else
  MAC="$(cat "$MAC_FILE" 2>/dev/null || true)"
  LINK="$1"
  if [[ -z $MAC ]]; then
    echo "No headset remembered yet. Run: sudo $0 scan" >&2
    echo "Then: sudo $0 <MAC> <spotify-link>" >&2
    exit 1
  fi
fi

connect_headset "$MAC"

[[ -n ${LINK:-} ]] || { echo "Headset connected. Add a Spotify link to play something."; exit 0; }

wait_for_api

if ! grep -q '"username"' "$(getent passwd "${SUDO_USER:-pi}" | cut -d: -f6)/.config/go-librespot/state.json" 2>/dev/null; then
  echo "WARNING: not logged in to Spotify yet — run install.sh and pick the device in the Spotify app." >&2
fi

URI="$(link_to_uri "$LINK")"
echo "==> Playing $URI"
curl -sf -X POST "$API/player/play" \
  -H 'Content-Type: application/json' \
  -d "{\"uri\": \"$URI\"}"
show_status
