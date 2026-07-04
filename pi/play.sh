#!/usr/bin/env bash
#
# TapBox test rig — connect a Bluetooth headset/speaker and play from Spotify.
# Requires install.sh to have been run (and Spotify login completed).
#
# Usage:
#   sudo ./play.sh connect                # auto-find device in pairing mode, pair + connect
#   sudo ./play.sh connect "jbl"          # same, but match device name (if several found)
#   sudo ./play.sh <spotify-link>         # play (auto-connects remembered/nearby device)
#   sudo ./play.sh AA:BB:CC:DD:EE:FF <spotify-link>   # explicit MAC still works
#   sudo ./play.sh scan                   # list everything seen during a scan
#   sudo ./play.sh test                   # play a test sound through the headset (no Spotify)
#   sudo ./play.sh pause | resume | next | prev | stop
#
# <spotify-link> can be a share link (https://open.spotify.com/track/...),
# a short link (https://spotify.link/...), or a spotify:track:... URI.
# Track, album, playlist and artist links all work.
#
# Non-Spotify links play via mpv (foreground — Ctrl+C stops): NRK podcast
# episodes/feeds (radio.nrk.no/podkast/...), NRK series (radio.nrk.no/serie/...),
# RSS feed URLs, direct stream URLs, or local file paths.

set -euo pipefail

API="http://127.0.0.1:3678"
MAC_FILE="/etc/tapbox/bt-headset"
MAC_RE='^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$'
SCAN_SECS=20

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $*" >&2
  exit 1
fi

usage() {
  cat >&2 <<EOF
Usage:
  sudo $0 connect [name]        pair + connect headset/speaker (pairing mode on!)
  sudo $0 <spotify-link>        play a track/album/playlist link
  sudo $0 <MAC> <spotify-link>  explicit device MAC
  sudo $0 scan                  list all devices seen during a scan
  sudo $0 test                  play a test sound through the headset (no Spotify)
  sudo $0 pause|resume|next|prev|stop
EOF
  exit 1
}

strip_ansi() { sed -E $'s/\x1B\\[[0-9;]*[A-Za-z]//g'; }

# Radio can be rfkill-blocked (persists across reboots on a fresh install),
# which makes every scan come up empty. Unblock before powering on.
bt_up() {
  rfkill unblock bluetooth 2>/dev/null || true
  bluetoothctl power on >/dev/null
  # Without pairable on, BlueZ does a non-bonding pairing (new_link_key with
  # store_hint 0): "Pairing successful" but the key is thrown away, so the
  # bond is gone after a power cycle and reconnect never works.
  bluetoothctl pairable on >/dev/null
}

# Scan for SCAN_SECS and print one line per device actually seen during the
# scan: "MAC<TAB>NAME<TAB>yes|no" (third field: looks like an audio device).
# Note: RSSI/UUID info is unreliable for unpaired devices, so "no" just means
# "could not confirm audio", not "definitely not audio".
discover() {
  bt_up
  echo "Scanning ${SCAN_SECS}s — put the speaker/headset in pairing mode now..." >&2
  local out macs mac name info audio
  out="$(bluetoothctl --timeout "$SCAN_SECS" scan on 2>/dev/null | strip_ansi || true)"
  # Every device seen produces "[NEW] Device MAC ..." or "[CHG] Device MAC RSSI: ..."
  macs="$(sed -nE 's/.*Device (([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}).*/\1/p' <<<"$out" \
          | tr '[:lower:]' '[:upper:]' | sort -u)"
  for mac in $macs; do
    info="$(bluetoothctl info "$mac" 2>/dev/null | strip_ansi)" || continue
    name="$(sed -n 's/^[[:space:]]*Name: //p' <<<"$info" | head -n1)"
    [[ -n $name ]] || name="(no name)"
    if grep -qiE 'Icon: audio|Audio Sink|0000110b' <<<"$info"; then
      audio=yes
    else
      audio=no
    fi
    printf '%s\t%s\t%s\n' "$mac" "$name" "$audio"
  done
}

print_devices() {  # pretty-print discover() output
  awk -F'\t' '{ printf "  %s  %s%s\n", $1, $2, ($3 == "yes" ? "   [audio]" : "") }' <<<"$1" >&2
}

connect_headset() {
  local mac="$1"
  bt_up

  if ! bluetoothctl info "$mac" 2>/dev/null | grep -q "Paired: yes"; then
    echo "==> Pairing with $mac (make sure it is in pairing mode)..."
    if ! bluetoothctl pair "$mac"; then
      # Typically auth failed 0x05: the device holds a stale key from an
      # earlier half-pairing. Clear our side and pair fresh.
      echo "==> Pairing failed — clearing stale bond and retrying once..."
      bluetoothctl remove "$mac" >/dev/null 2>&1 || true
      sleep 2
      bluetoothctl --timeout 10 scan on >/dev/null 2>&1 || true
      bluetoothctl pair "$mac"
    fi
    bluetoothctl trust "$mac"
  fi

  # Always request a profile connect: right after pairing the device shows
  # "Connected: yes" from the pairing link itself, but A2DP is not up yet.
  echo "==> Connecting (A2DP) to $mac..."
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

  # The real "connected" test: bluealsa must expose an A2DP PCM for the device.
  echo -n "==> Waiting for audio transport"
  local ready=""
  for _ in $(seq 1 15); do
    if bluealsa-aplay -L 2>/dev/null | grep -qi "$mac"; then ready=1; break; fi
    echo -n "."
    sleep 1
  done
  echo
  if [[ -n $ready ]]; then
    echo "==> Audio transport ready."
  else
    echo "WARNING: bluetooth connected, but no A2DP audio transport appeared." >&2
    echo "Debug with: bluealsa-aplay -L   and   journalctl -u bluealsa -n 20" >&2
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

# Find a device automatically (optionally filtered by name), then connect.
auto_connect() {
  local filter="${1:-}"
  local seen candidates count
  seen="$(discover)"

  if [[ -z $seen ]]; then
    echo "No bluetooth devices seen at all. Check that the device is in pairing" >&2
    echo "mode and close to the Pi, then try again." >&2
    exit 1
  fi

  if [[ -n $filter ]]; then
    candidates="$(grep -iF "$filter" <<<"$seen" || true)"
    if [[ -z $candidates ]]; then
      echo "Nothing matching '$filter'. Devices seen during scan:" >&2
      print_devices "$seen"
      exit 1
    fi
  else
    candidates="$(awk -F'\t' '$3 == "yes"' <<<"$seen")"
    if [[ -z $candidates ]]; then
      echo "Saw these devices, but none confirmed as audio (some speakers only" >&2
      echo "advertise their audio profile after pairing):" >&2
      print_devices "$seen"
      echo "Pick yours by name: sudo $0 connect \"<name>\"" >&2
      exit 1
    fi
  fi

  count="$(grep -c . <<<"$candidates")"
  if [[ $count -gt 1 ]]; then
    echo "Multiple candidates found:" >&2
    print_devices "$candidates"
    echo "Pick one by name: sudo $0 connect \"<name>\"" >&2
    exit 1
  fi

  local mac name
  mac="$(cut -f1 <<<"$candidates")"
  name="$(cut -f2 <<<"$candidates")"
  echo "==> Found device: $name ($mac)"
  connect_headset "$mac"
}

# Connect whatever we know about: remembered device, else auto-discover.
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

play_mpv() {
  local link="$1"
  # Expand NRK/RSS links to stream URLs via nrk.py (repo copy first, then installed)
  mapfile -t urls < <(python3 - "$link" <<PYEOF
import sys
# Repo copy must win over the installed copy — insert(0, ...) last = first
sys.path.insert(0, "/usr/local/bin")
sys.path.insert(0, "$(dirname "$(readlink -f "$0")")")
try:
    import nrk
    print(f"using {nrk.__file__}", file=sys.stderr)
    urls = nrk.expand(sys.argv[1])
except Exception as e:
    print(f"nrk expansion failed: {e}", file=sys.stderr)
    urls = [sys.argv[1]]
print("\n".join(urls))
PYEOF
  )
  curl -sf -X POST "$API/player/pause" >/dev/null 2>&1 || true
  # Podcast link? Cache the newest episodes in the background for offline use.
  if [[ $link =~ radio\.nrk\.no/podkast/([a-zA-Z0-9_-]+) ]]; then
    local nrkpy
    nrkpy="$(dirname "$(readlink -f "$0")")/nrk.py"
    [[ -f $nrkpy ]] || nrkpy=/usr/local/bin/nrk.py
    nohup python3 "$nrkpy" sync "${BASH_REMATCH[1]}" \
      >> /var/log/tapbox-sync.log 2>&1 &
    echo "==> Background sync started (newest 50 episodes -> /var/lib/tapbox/cache)"
  fi
  echo "==> Playing ${#urls[@]} stream(s) via mpv (Ctrl+C to stop)"
  mpv --no-video --really-quiet --audio-device=alsa/tapbox_bt "${urls[@]}"
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
    seen="$(discover)"
    echo "Devices seen during scan:"
    print_devices "${seen:-}"
    exit 0 ;;
  connect)
    auto_connect "${2:-}"
    echo "Device connected and set as output. Play with: sudo $0 <spotify-link>"
    exit 0 ;;
  test)
    ensure_headset
    echo "==> Playing test sound (you should hear 'Front Center')..."
    aplay -D tapbox_bt /usr/share/sounds/alsa/Front_Center.wav
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

[[ -n ${LINK:-} ]] || { echo "Device connected. Add a link to play something."; exit 0; }

if [[ $LINK =~ ^spotify: || $LINK == *open.spotify.com* || $LINK == *spotify.link/* ]]; then
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
else
  play_mpv "$LINK"
fi
