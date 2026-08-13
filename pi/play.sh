#!/usr/bin/env bash
#
# Vibb test rig — connect a Bluetooth headset/speaker and play from Spotify.
# Requires install.sh to have been run (and Spotify login completed).
#
# Usage:
#   sudo ./play.sh connect                # auto-find device in pairing mode, pair + connect
#   sudo ./play.sh connect "jbl"          # same, but match device name (if several found)
#   sudo ./play.sh <spotify-link>         # play IN BACKGROUND via the daemon
#   sudo ./play.sh --fresh <link>         # ignore remembered position, start from the top
#   sudo ./play.sh --fg <link>            # play in the foreground (dev; Ctrl+C stops)
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
MAC_FILE="/etc/vibb/bt-headset"
MAC_RE='^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$'

# All bluetooth logic lives in vibb/bt.py (shared with vibbd's /bt API)
BT_PY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/vibb/bt.py"
[[ -f $BT_PY ]] || BT_PY=/usr/local/lib/vibb-py/vibb/bt.py
bt_py() { python3 "$BT_PY" "$@"; }

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $*" >&2
  exit 1
fi

DAEMON="http://127.0.0.1:3679"

# The box's API token. Playing a RAW url is privileged (it can put
# uncurated audio in a kid's room), so this CLI has to authenticate like
# any other client — see SECURITY.md. Root-only file; an empty value
# just means the privileged calls get a clear 401 instead of silently
# doing nothing.
TOKEN_HDR=()
if [[ -r ${VIBB_TOKEN_FILE:-/etc/vibb/api-token} ]]; then
  TOKEN_HDR=(-H "X-Vibb-Token: $(tr -d '[:space:]' \
    < "${VIBB_TOKEN_FILE:-/etc/vibb/api-token}")")
fi
FRESH_ARG=""
FG=0
while [[ ${1:-} == "--fresh" || ${1:-} == "--fg" ]]; do
  [[ $1 == "--fresh" ]] && FRESH_ARG="--fresh"
  [[ $1 == "--fg" ]] && FG=1
  shift
done

usage() {
  cat >&2 <<EOF
Usage:
  sudo $0 connect [name]        pair + connect headset/speaker (pairing mode on!)
  sudo $0 use <MAC>             connect a known device + route audio to it
  sudo $0 forget <MAC>          remove the bond (and config if it was active)
  sudo $0 <spotify-link>        play a track/album/playlist link
  sudo $0 <MAC> <spotify-link>  explicit device MAC
  sudo $0 scan                  list all devices seen during a scan
  sudo $0 test                  play a test sound through the headset (no Spotify)
  sudo $0 pause|resume|next|prev|stop
  sudo $0 vol [0-100 | +N | -N]  show or set the box volume
EOF
  exit 1
}

wait_for_api() {
  for _ in $(seq 1 30); do
    curl -sf "$API/status" >/dev/null && return 0
    sleep 1
  done
  echo "go-librespot API not reachable at $API — check: journalctl -u go-librespot -n 50" >&2
  exit 1
}

[[ $# -ge 1 ]] || usage

case "$1" in
  scan)
    bt_py scan
    exit 0 ;;
  connect)
    bt_py connect ${2:+"$2"}
    echo "Device connected and set as output. Play with: sudo $0 <spotify-link>"
    exit 0 ;;
  test)
    bt_py ensure
    echo "==> Playing test sound (you should hear 'Front Center')..."
    aplay -D vibb_bt /usr/share/sounds/alsa/Front_Center.wav
    exit 0 ;;
  pause|resume|next|prev|stop)
    # Route via the daemon so the command hits whatever is actually active
    case "$1" in
      pause)  EP=pause ;;      # pure pause (never toggles into playing)
      resume) EP=playpause ;;
      *)      EP="$1" ;;
    esac
    if curl -sf -X POST "$DAEMON/$EP" -H 'Content-Type: application/json' \
         "${TOKEN_HDR[@]}" -d '{}'; then
      echo " OK: $1"
    else
      echo "daemon not running — falling back to Spotify API" >&2
      wait_for_api
      curl -sf -X POST "$API/player/$1" >/dev/null && echo "OK: $1 (spotify only)"
    fi
    exit 0 ;;
  scan-raw)
    # Machine-readable scan for vibbd /bt/scan: mac<TAB>name<TAB>audio
    bt_py scan-raw
    exit 0 ;;
  use)
    [[ ${2:-} =~ $MAC_RE ]] || { echo "usage: sudo $0 use <MAC>" >&2; exit 1; }
    bt_py use "$2"
    exit 0 ;;
  forget)
    [[ ${2:-} =~ $MAC_RE ]] || { echo "usage: sudo $0 forget <MAC>" >&2; exit 1; }
    bt_py forget "$2"
    exit 0 ;;
  vol)
    if [[ -z ${2:-} ]]; then
      curl -sf "$DAEMON/volume" | jq . || echo "daemon not running" >&2
    else
      case "$2" in
        +*|-*) BODY="{\"delta\": $2}" ;;
        *)     BODY="{\"volume\": $2}" ;;
      esac
      curl -sf -X POST "$DAEMON/volume" -H 'Content-Type: application/json' \
      "${TOKEN_HDR[@]}" \
        -d "$BODY" | jq . || echo "daemon not running" >&2
    fi
    exit 0 ;;
  status)
    curl -sf "$DAEMON/status" | jq . || echo "daemon not running" >&2
    exit 0 ;;
esac

# Remaining forms: <link>  or  <MAC> <link>
if [[ $1 =~ $MAC_RE ]]; then
  bt_py use "$1"
  LINK="${2:-}"
else
  LINK="$1"
  bt_py ensure
fi

[[ -n ${LINK:-} ]] || { echo "Device connected. Add a link to play something."; exit 0; }

# All link routing lives in player.py: Spotify -> go-librespot, rest -> mpv
if [[ $LINK =~ ^spotify: || $LINK == *open.spotify.com* || $LINK == *spotify.link/* ]]; then
  wait_for_api
  if ! grep -q '"username"' "$(getent passwd "${SUDO_USER:-pi}" | cut -d: -f6)/.config/go-librespot/state.json" 2>/dev/null; then
    echo "WARNING: not logged in to Spotify yet — run install.sh and pick the device in the Spotify app." >&2
  fi
fi

if [[ $FG -eq 1 ]]; then
  PLAYERPY="$(dirname "$(readlink -f "$0")")/player.py"
  [[ -f $PLAYERPY ]] || PLAYERPY=/usr/local/bin/vibb-player
  # shellcheck disable=SC2086
  python3 "$PLAYERPY" $FRESH_ARG "$LINK"
  exit 0
fi

FRESH_BOOL=false
[[ -n $FRESH_ARG ]] && FRESH_BOOL=true
if curl -sf -X POST "$DAEMON/play" -H 'Content-Type: application/json' \
     "${TOKEN_HDR[@]}" \
     -d "{\"target\": \"$LINK\", \"fresh\": $FRESH_BOOL}" >/dev/null; then
  echo "==> Playing in the background (survives this terminal)."
  echo "    Follow:  journalctl -u vibb-daemon -f"
  echo "    Control: sudo $0 pause|next|prev|stop   or the buttons"
else
  echo "daemon not running — playing in the foreground instead" >&2
  PLAYERPY="$(dirname "$(readlink -f "$0")")/player.py"
  [[ -f $PLAYERPY ]] || PLAYERPY=/usr/local/bin/vibb-player
  # shellcheck disable=SC2086
  python3 "$PLAYERPY" $FRESH_ARG "$LINK"
fi
