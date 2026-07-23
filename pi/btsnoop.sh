#!/usr/bin/env bash
# TapBox btsnoop ring — diagnostic HCI capture for the BCM43430 crash hunt.
#
# Purpose: decide WHICH LAYER owns `hci0: hardware error 0x00`. The kernel
# SYNTHESIZES that exact event (hci_reset_dev, code 0x00) when the HOST gives
# up on the UART link — so the dmesg line alone cannot distinguish "the BT
# core's firmware faulted" from "the UART link desynced and the kernel
# injected the error". The btsnoop trace can: a REAL controller fault shows
# clean HCI traffic ending in a genuine Hardware Error event from the chip;
# a link fault shows garbage/malformed frames (or silence) before a
# host-injected one. bluez#1170 (same signature, fixed by a UART swap) makes
# this distinction decision-critical: link-owned -> miniuart-bt/dongle
# territory; chip-owned -> firmware/coex territory.
#
# Ring design: full HCI snoop includes the A2DP payload (~150 MB/hour), so
# segments live in RAM (/run) and only the newest few are kept — the SD card
# is never touched and RAM use is bounded. On a crash the box keeps running
# (wifi/SSH survive), so copy the segments out BEFORE rebooting:
#     cp /run/tapbox-btsnoop/*.snoop ~/   # then analyze: btmon -r <file>
#
# Opt-in: installed disabled; enable for a hunt with
#     sudo systemctl enable --now tapbox-btsnoop
set -u

DIR="${TAPBOX_BTSNOOP_DIR:-/run/tapbox-btsnoop}"
SEG_S="${TAPBOX_BTSNOOP_SEG_S:-300}"   # segment length (s)
KEEP="${TAPBOX_BTSNOOP_KEEP:-3}"       # segments kept (~12MB each under A2DP)

mkdir -p "$DIR"
echo "btsnoop ring: ${SEG_S}s segments, keeping $KEEP, in $DIR (RAM)"
while true; do
  f="$DIR/$(date +%Y%m%d-%H%M%S).snoop"
  timeout "$SEG_S" btmon -w "$f" >/dev/null 2>&1
  rc=$?
  # 124 = segment completed (timeout expired) — normal. Anything else means
  # btmon itself failed (no adapter, no btmon binary): back off, don't spin.
  [ "$rc" -ne 124 ] && sleep 30
  ls -1t "$DIR"/*.snoop 2>/dev/null | tail -n +$((KEEP + 1)) \
    | xargs -r rm -f
done
