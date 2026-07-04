#!/usr/bin/env bash
#
# TapBox test rig — connect a Bluetooth headset and play from Spotify.
# Requires install.sh to have been run (and Spotify login completed).
#
# Usage:
#   sudo ./play.sh connect                # auto-find headset in pairing mode, pair + connect
#   sudo ./play.sh connect "jbl"          # same, but match device name (if several found)
#   sudo ./play.sh <spotify-link>         # play (auto-connects remembered/nearby headset)
#   sudo ./play.sh AA:BB:CC:DD:EE:FF <spotify-link>   # explicit MAC still works
#   sudo ./play.sh scan                   # raw scan, list everything with MACs
#   sudo ./play.sh pause | resume | next | prev | stop
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

usage() {
  cat >&2 <<EOF
Usage:
  sudo $0 connect [name]        pair + connect headset (pairing mode on!)
  sudo $0 <spotify-link>        play a track/album/playlist link
  sudo $0 <MAC> <spotify-link>  explicit headset MAC
  sudo $0 scan                  list all visible bluetooth devices
  sudo $0 pause|resume|next|prev|stop
EOF
  exit 1
}

scan() {
  bluetoothctl power on >/dev/null
  echo "Scanning for 15s — put your headset in pairing mode now..."
  bluetoothctl --timeout 15 scan on >/dev/null 2>&1 || true
  echo
  echo "Devices found (name + MAC):"
  bluetoothctl devices
}

# Prints "MAC<TAB>NAME" for nearby audio devices (A2DP sinks / headsets).
discover_audio() {
  bluetoothctl power on >/dev/null
  echo "Scanning 15s for audio devices — put your headset in pairing mode..." >&2
  bluetoothctl --timeout 15 scan on >/dev/null 2>&1 || true
  local mac name info
  while read -r _ mac name; do
    info="$(bluetoothctl info "$mac" 2>/dev/null)" || continue
    # must look like an audio device (headset icon or A2DP sink UUID)
    grep -qiE 'Icon: audio|Audio Sink|0000110b' <<<"$info" || continue
    # must be nearby right now (RSSI from this scan) or already paired
    grep -qE 'RSSI:|Paired: yes' <<<"$info" || continue
    printf '%s\t%s\n' "$mac" "$name"
  done < <(bluetoothctl devices)
}

connect_headset() {
  local mac="$1"
  bluetoothctl power on >/dev/null

  if ! bluetoothctl info "$mac" 2>/dev/null | grep -q "Paired: yes"; then
    echo "==> Pairing with $mac (make sure it is in pairing mode)..."
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

  mkdir -p "$(dirname "$MAC_FILE")"
  echo "$mac" > "$MAC_FILE"

  # Point the tapbox_bt ALSA device at this headset
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
    echo "==> ALSA output routed to $mac, restarting go-librespot..."
    systemctl restart go-librespot
  fi
}

# Find a headset automatically (optionally filtered by name), then connect.
auto_connect() {
  local filter="${1:-}"
  local candidates
  if [[ -n $filter ]]; then
    candidates="$(discover_audio | grep -iF "$filter" || true)"
  else
    candidates="$(discover_audio)"
  fi

  local count
  count="$(grep -c . <<<"$candidates" || true)"
  [[ -n $candidates ]] || count=0

  if [[ $count -eq 0 ]]; then
    echo "No audio devices found. Is the headset in pairing mode?" >&2
    echo "See everything nearby with: sudo $0 scan" >&2
    exit 1
  elif [[ $count -gt 1 ]]; then
    echo "Multiple audio devices found:" >&2
    sed 's/^/  /' <<<"$candidates" >&2
    echo "Pick one by name: sudo $0 connect \"<name>\"" >&2
    exit 1
  fi

  local mac name
  mac="${candidates%%$'\t'*}"
  name="${candidates#*$'\t'}"
  echo "==> Found headset: $name ($mac)"
  connect_headset "$mac"
}

# Connect whatever we know about: remembered headset, else auto-discover.
ensure_headset() {
  local mac
  mac="$(cat "$MAC_FILE" 2>/dev/null || true)"
  if [[ -n $mac ]]; then
    connect_headset "$mac"
  else
    auto_connect ""
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
  connect)
    auto_connect "${2:-}"
    echo "Headset connected and set as output. Play with: sudo $0 <spotify-link>"
    exit 0 ;;
  pause|resume|next|prev|stop)
    wait_for_api
    curl -sf -X POST "$API/player/$1" >/dev/null && echo "OK: $1"
    exit 0 ;;
esac

# Remaining forms: <link>  or  <MAC> <link>
if [[ $1 =~ $MAC_RE ]]; then
  connect_headset "$1"
  LINK="${2:-}"
else
  LINK="$1"
  ensure_headset
fi

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
