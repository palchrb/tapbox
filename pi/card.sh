#!/usr/bin/env bash
#
# Manage TapBox card mappings (installed as tapbox-card).
#   sudo tapbox-card map <link-or-path>   next tapped card plays this
#   sudo tapbox-card list                 show all mappings
#   sudo tapbox-card forget <uid>         remove one mapping
#   sudo tapbox-card cancel               abort a pending map
#
# Targets: Spotify links, NRK links (radio.nrk.no serie/podkast), RSS feed
# URLs, direct stream URLs, or local file paths.

set -euo pipefail

CARDS=/etc/tapbox/cards.json
PENDING=/etc/tapbox/pending-map

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 $*" >&2
  exit 1
fi

case "${1:-}" in
  map)
    [[ $# -ge 2 ]] || { echo "usage: sudo $0 map <link-or-path>" >&2; exit 1; }
    mkdir -p /etc/tapbox
    printf '%s\n' "$2" > "$PENDING"
    echo "Tap a card on the reader now — it will be mapped to:"
    echo "  $2"
    ;;
  list)
    jq . "$CARDS" 2>/dev/null || echo "{}"
    ;;
  forget)
    [[ $# -ge 2 ]] || { echo "usage: sudo $0 forget <uid>" >&2; exit 1; }
    tmp="$(mktemp)"
    jq --arg uid "$2" 'del(.[$uid])' "$CARDS" > "$tmp" && mv "$tmp" "$CARDS"
    echo "Forgot card $2"
    ;;
  cancel)
    rm -f "$PENDING"
    echo "Pending mapping cancelled."
    ;;
  *)
    sed -n '4,11p' "$0"
    exit 1
    ;;
esac
