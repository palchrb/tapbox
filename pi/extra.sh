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
RFKILL="${TAPBOX_RFKILL:-rfkill}"
IW="${TAPBOX_IW:-iw}"
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

CPUS="${TAPBOX_CPUFREQ:-/sys/devices/system/cpu}"
GOV_STATE="${TAPBOX_RUN:-/run}/tapbox-extra-governor"
# One human line for the box screen: tapbox-ui shows-and-deletes this on
# its next start (docs/extras.md). Scripts write their own reason;
# --restore fills in a generic one when the unit failed silently.
MSG_FILE="${TAPBOX_RUN:-/run}/tapbox-extra.msg"

case "${1:-}" in
  --run)
    script="${2:?usage: tapbox-extra --run <script>}"
    rm -f "$MSG_FILE" 2>/dev/null || true  # no stale note from last time
    # Unpark the CPU: boot runs 'tapbox-power save', which pins the
    # governor to powersave (= 600 MHz flat on the Zero 2 W) — great
    # for podcasts, hopeless for an emulator. Snapshot whatever mode
    # the box was in, lift to ondemand for the extra; --restore puts
    # the snapshot back (default powersave — the safe battery state).
    prev="$(cat "$CPUS"/cpu0/cpufreq/scaling_governor 2>/dev/null || true)"
    [ -n "$prev" ] && { echo "$prev" > "$GOV_STATE" 2>/dev/null || true; }
    for g in "$CPUS"/cpu*/cpufreq/scaling_governor; do
      echo ondemand > "$g" 2>/dev/null || true
    done
    # Stop playback first — keep:true preserves the position bookmark
    # (a plain /stop clears it: 'stop = start over' is the kid-facing
    # semantic; the handoff must not cost the audiobook position —
    # field 2026-07-29). SAFE endpoint; CSRF gate wants the JSON type.
    curl -s -m 5 -X POST -H 'Content-Type: application/json' \
         -d '{"keep":true}' "$API/stop" >/dev/null 2>&1 || true
    $SYSTEMCTL stop $HANDOFF
    $SYSTEMCTL stop go-librespot 2>/dev/null || true  # frees I2S/ALSA
    exec "$script"
    ;;
  --restore)
    # A silent failure still deserves a word on the screen: systemd
    # hands ExecStopPost the unit's outcome — if the script died
    # without leaving its own message, write a generic one.
    if [ "${SERVICE_RESULT:-success}" != success ] && [ ! -s "$MSG_FILE" ]; then
      echo "Extra failed (${EXIT_STATUS:-?}) — see journalctl -u tapbox-extra" \
        > "$MSG_FILE" 2>/dev/null || true
    fi
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
    # Radio baseline: extras (games especially) often rfkill wifi for
    # coex/latency on the shared radio. systemd-rfkill PERSISTS a block
    # across reboots, so a crashed script could leave the box offline
    # for good — the return trip always hands back both radios.
    $RFKILL unblock wifi bluetooth 2>/dev/null || true
    # ...and undo a script's wifi softening: a fixed txpower would
    # otherwise persist into normal operation (power_save is already
    # governed dynamically by tapboxd, no reset needed there)
    $IW dev wlan0 set txpower auto 2>/dev/null || true
    # re-park the CPU to whatever mode --run found (battery default)
    prev="$(cat "$GOV_STATE" 2>/dev/null || echo powersave)"
    for g in "$CPUS"/cpu*/cpufreq/scaling_governor; do
      echo "$prev" > "$g" 2>/dev/null || true
    done
    rm -f "$GOV_STATE" 2>/dev/null || true
    ;;
  *)
    echo "usage: tapbox-extra --run <script> | --restore" >&2
    exit 2
    ;;
esac
