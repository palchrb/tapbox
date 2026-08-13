#!/usr/bin/env bash
#
# Vibb API token (installed as vibb-token).
#
#   sudo vibb-token          show the token + the link that pairs a phone
#   sudo vibb-token rotate   issue a NEW token (every linked phone must
#                              be linked again)
#   sudo vibb-token path     print just the file path
#
# The token gates the privileged half of the box API (Wi-Fi, Bluetooth,
# settings, shutdown). Playback and the PWA's read-only views never need
# it. The normal way to pair a phone is the box's own screen —
# Settings -> Link phone -> scan the QR — which proves you are standing
# at the box. This command is the fallback for a broken screen, a
# headless box, or a quick look over SSH; it exists as a SEPARATE command
# on purpose, so the secret is only ever printed when you ask for it and
# doesn't end up in the install log or your scrollback by accident.
#
# See SECURITY.md for the threat model.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0 ${1:-}" >&2
  exit 1
fi

PYLIB=/usr/local/lib/vibb-py
TOKEN_FILE="${VIBB_TOKEN_FILE:-/etc/vibb/api-token}"

py() {  # one implementation of the token rules — the daemon's own module
  VIBB_TOKEN_FILE="$TOKEN_FILE" /usr/bin/python3 -c "
import sys
sys.path.insert(0, '$PYLIB')
from vibb import token
$1"
}

box_name() {
  local n=""
  [[ -f /etc/avahi/avahi-daemon.conf ]] \
    && n="$(sed -n 's/^host-name=//p' /etc/avahi/avahi-daemon.conf | head -n1)"
  echo "${n:-$(hostname -s)}"
}

show() {
  local tok name ip
  tok="$(py 'print(token.ensure())')" || {
    echo "Could not read $TOKEN_FILE — is Vibb installed?" >&2; exit 1; }
  name="$(box_name)"
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "  Box token:  $(py "print(token.grouped('$tok'))")"
  echo
  echo "  Pair a phone by opening:"
  # The .local form first: the browser stores the token per ORIGIN, so a
  # link opened against an IP is lost the moment DHCP moves the box.
  echo "      http://${name}.local:3679/#t=${tok}"
  [[ -n $ip ]] && echo "      (or http://${ip}:3679/#t=${tok} — if .local won't resolve)"
  echo
  echo "  Normally you'd scan the QR on the box: Settings -> Link phone."
  echo
}

case "${1:-show}" in
  show|"")
    show
    ;;
  rotate)
    echo "This issues a NEW token. Every phone linked to this box will"
    echo "stop working until it is linked again."
    read -r -p "Rotate the token? [y/N] " ans
    [[ ${ans,,} == y || ${ans,,} == yes ]] || { echo "Cancelled."; exit 0; }
    py 'token.rotate()'
    echo "Rotated."
    show
    ;;
  path)
    echo "$TOKEN_FILE"
    ;;
  *)
    sed -n '3,19p' "$0"
    exit 1
    ;;
esac
