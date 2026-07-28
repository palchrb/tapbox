#!/usr/bin/env bash
# tapbox-extra — hand the box to an owner script; guarantee the return.
#
# Launched by the screen UI (hold X+Y -> Extras -> confirm) as a
# TRANSIENT systemd unit:
#
#   systemd-run --unit=tapbox-extra --collect --property=Restart=no \
#     --property='ExecStopPost=/usr/local/bin/tapbox-extra --restore' \
#     /usr/local/bin/tapbox-extra --run <script>
#
# ExecStopPost is the return guarantee: systemd runs it however the
# main process dies — clean exit, crash, OOM, even SIGKILL of this
# wrapper itself. A shell trap cannot promise that (QA 2026-07-28).
#
# Contract with the owner script (docs/extras.md): it owns the display,
# the buttons and the audio device until it EXITS. It may stop MORE
# tapbox services itself (never disable/mask them) — the restore set
# below starts everything back deterministically, including units the
# wrapper never stopped, so a script that stopped tapbox-daemon for RAM
# still returns to a whole box.
set -u

SYSTEMCTL="${TAPBOX_SYSTEMCTL:-systemctl}"
API="${TAPBOX_DAEMON:-http://127.0.0.1:3679}"

# Stopped on handoff: the display/button owner, the auto-power-off (a
# game session has neither playback nor box-button activity and must
# not be shut down under the player), and the media-key grabber (a USB
# gamepad must not be half-eaten). go-librespot holds the ALSA device.
# tapbox-daemon deliberately STAYS UP: it holds no hardware, and its
# API is the remote escape hatch (battery, POST /system/shutdown from
# a linked phone) while the extra runs. Low-battery poweroff (PiSugar)
# is untouched.
HANDOFF="tapbox-idle tapbox-buttons tapbox-ui"
# The restore set is the whole audio chain, not just what --run stopped:
# a script may stop bluetooth/bluealsa to use the radio itself, and the
# box must still come back whole. Anything OUTSIDE this set that a
# script stops is the script's own business.
RESTORE="bluetooth bluealsa go-librespot tapbox-daemon tapbox-mpris
         tapbox-bt-reconnect tapbox-buttons tapbox-idle tapbox-ui"

case "${1:-}" in
  --run)
    script="${2:?usage: tapbox-extra --run <script>}"
    # Stop playback first — the bookmark machinery preserves the exact
    # episode/track position for the return. SAFE endpoint; the CSRF
    # gate wants the JSON content type.
    curl -s -m 5 -X POST -H 'Content-Type: application/json' -d '{}' \
         "$API/stop" >/dev/null 2>&1 || true
    $SYSTEMCTL stop $HANDOFF
    $SYSTEMCTL stop go-librespot 2>/dev/null || true  # frees I2S/ALSA
    exec "$script"
    ;;
  --restore)
    # unmask FIRST: a script that masked units would otherwise survive
    # the start below and brick the box (QA invariant). Then re-enable:
    # start heals NOW, but a script's 'disable' would survive to the
    # next boot — the contract says never disable, this is the belt.
    # (tapbox-btsnoop is deliberately outside the set: it ships
    # disabled/opt-in and must stay whatever the owner chose.)
    $SYSTEMCTL unmask $RESTORE >/dev/null 2>&1 || true
    $SYSTEMCTL enable $RESTORE >/dev/null 2>&1 || true
    for u in $RESTORE; do
      $SYSTEMCTL start "$u" 2>/dev/null || true
    done
    ;;
  *)
    echo "usage: tapbox-extra --run <script> | --restore" >&2
    exit 2
    ;;
esac
