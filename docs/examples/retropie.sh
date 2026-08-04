#!/usr/bin/env bash
# tapbox-name: RetroPie
#
# TapBox edition of palchrb/retropie_wrapper — launched by the extras
# hook (hold X+Y -> Extras -> A), NOT by BT-controller connections, and
# with the SPI display instead of CEC/TV. Kept from the original: the
# real-CLI-session-via-screen requirement, and the emergency hold-
# button (hold the controller's MODE/home button 3s -> Ctrl-C into the
# session when a game wedges).
#
# Install ON THE BOX (SSH only):
#   sudo cp retropie.sh /etc/tapbox/extras/retropie.sh
#   sudo chown root:root /etc/tapbox/extras/retropie.sh
#   sudo chmod 755 /etc/tapbox/extras/retropie.sh
# Deps: apt install screen python3-evdev  (+ build fbcp-ili9341)
#
# The tapbox-extra wrapper has ALREADY: stopped tapbox-ui/idle/buttons,
# stopped playback (bookmarked) and go-librespot, and lifted the CPU
# governor. On exit — however this script ends — the wrapper restores
# every TapBox service, rfkill-unblocks both radios and resets wifi
# txpower, so nothing here is load-bearing for the return trip.
set -u
RP_USER="${RP_USER:-palchrb}"   # the user RetroPie-Setup installed for
SESSION=retropie

# --- first-run dependency bootstrap -----------------------------------
# MUST run before the RF-quiet block below cuts wifi. Idempotent: once
# everything is present this is a no-op. RetroPie itself is NOT
# auto-installed — that's a deliberate owner action (RetroPie-Setup).
need=""
command -v screen >/dev/null || need="$need screen"
python3 -c "import evdev" 2>/dev/null || need="$need python3-evdev"
command -v cec-client >/dev/null || need="$need cec-utils"
if [ -n "$need" ]; then
  echo "retropie: first run — installing:$need"
  apt-get update -qq && apt-get install -y -qq $need \
    || echo "retropie: WARNING: dependency install failed (offline?) — continuing"
fi

# quiet the BT pager while pairing/using a BT controller (the wrapper's
# restore starts it again). Uncomment the daemon line if a core needs
# the RAM — the phone's remote escape hatch is gone while it's down.
systemctl stop tapbox-bt-reconnect 2>/dev/null || true
# systemctl stop tapbox-daemon tapbox-mpris

# RF quiet for gaming: the controller (HID) gets the shared radio to
# itself. Default: wifi OFF (no SSH until the session ends). With
# KEEP_WIFI=1 wifi stays up but is softened (power-save + 5 dBm) so you
# can watch live via:   screen -x retropie
if [ "${KEEP_WIFI:-0}" = 1 ]; then
  iw dev wlan0 set power_save on 2>/dev/null || true
  iw dev wlan0 set txpower fixed 500 2>/dev/null || true
else
  rfkill block wifi
fi
# hang up BT AUDIO sinks (JBL/car) — HID controllers are left alone;
# game sound goes out the I2S jack anyway
for d in $(bluetoothctl devices Connected 2>/dev/null | awk '{print $2}'); do
  bluetoothctl info "$d" | grep -q "Audio Sink" \
    && bluetoothctl disconnect "$d" >/dev/null
done

# --- display: is there a TV on HDMI? ----------------------------------
# Detection depends on which graphics stack the box booted:
#
#  * KMS (dtoverlay=vc4-kms-v3d): the DRM connector answers honestly.
#  * LEGACY firmware stack (no /sys/class/drm, only /dev/fb0 — this
#    box, field 2026-08-04): there is NO supported probe. tvservice,
#    the old answer, was removed from trixie. So we CANNOT tell.
#
# When we cannot tell, PROCEED. The previous version read the DRM
# connector unconditionally, found nothing on a legacy box, and
# aborted with "no TV found" every single time — the abort WAS the
# bug, and RetroPie never once got to start. Trusting the human is the
# right default here: launching this is a deliberate X+Y chord plus a
# confirm, the emergency MODE-hold still quits a session with no
# picture, and the wrapper's ExecStopPost brings TapBox back whatever
# happens. A wrong guess costs one confused minute; a wrong abort cost
# the whole feature.
#
# CEC then wakes the TV and grabs its input: 'as' (active source)
# makes the TV switch to WHICHEVER port the Pi occupies — CEC
# negotiates physical addresses over the cable, so no port is ever
# configured (the proven pair from palchrb/retropie_wrapper).
TV=0
FBCP_PID=""
hdmi="$(cat /sys/class/drm/card*-HDMI-A-*/status 2>/dev/null | head -1)"
if [ -z "$hdmi" ]; then
  hdmi=unknown   # legacy stack: no connector to ask
fi
if [ "$hdmi" != disconnected ]; then
  TV=1
  [ "$hdmi" = unknown ] && echo "retropie: no DRM connector (legacy" \
    "graphics stack) — cannot probe HDMI, assuming a TV is attached"
  if command -v cec-client >/dev/null; then
    echo 'on 0' | cec-client -s -d 1 >/dev/null 2>&1 || true
    echo 'as'   | cec-client -s -d 1 >/dev/null 2>&1 || true
  fi
else
  # The connector EXISTS and says nothing is plugged in — that is a
  # real answer, so the old abort still applies. Optional SPI
  # experiment: provide a mirror yourself via FBCP_BIN (classic
  # fbcp-ili9341 wants the legacy dispmanx stack — which, note, is
  # exactly what a box WITHOUT the vc4 overlay runs).
  FBCP_BIN="${FBCP_BIN:-/usr/local/bin/fbcp-ili9341}"
  if [ -x "$FBCP_BIN" ]; then
    "$FBCP_BIN" & FBCP_PID=$!
  else
    # leave the reason ON THE BOX SCREEN (tapbox-ui shows this file on
    # its next start), then bail so TapBox comes right back
    echo "RetroPie: no TV found — connect HDMI and try again" \
      > "${TAPBOX_RUN:-/run}/tapbox-extra.msg" 2>/dev/null || true
    echo "retropie: no TV on HDMI — aborting so TapBox comes right back"
    exit 1
  fi
fi

# Emergency button (ported from wrapper.py): hold BTN_MODE 3s -> one
# Ctrl-C into the screen session. Hotplug scan instead of a MAC list:
# any pad with a MODE button qualifies, also ones that connect later.
EMERG_PID=""
if python3 -c "import evdev" 2>/dev/null; then
  python3 - "$SESSION" <<'PYEOF' &
import subprocess, sys, threading, time
from evdev import list_devices, InputDevice, ecodes
SESSION, HOLD = sys.argv[1], 3
def ctrl_c():
    subprocess.run(["screen", "-S", SESSION, "-X", "stuff", "\x03"])
def watch(dev):
    timer = None
    try:
        for ev in dev.read_loop():
            if ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_MODE:
                if ev.value == 1 and timer is None:
                    timer = threading.Timer(HOLD, ctrl_c); timer.start()
                elif ev.value == 0 and timer:
                    timer.cancel(); timer = None
    except OSError:
        pass  # pad gone — the main loop re-adopts it on reconnect
seen = set()
while True:
    for p in list_devices():
        if p in seen:
            continue
        try:
            d = InputDevice(p)
            if ecodes.BTN_MODE in d.capabilities().get(ecodes.EV_KEY, []):
                seen.add(p)
                threading.Thread(target=watch, args=(d,), daemon=True).start()
        except OSError:
            pass
    time.sleep(2)
PYEOF
  EMERG_PID=$!
else
  echo "retropie: python3-evdev missing — emergency button disabled" \
       "(apt install python3-evdev)"
fi

cleanup() {
  [ -n "$EMERG_PID" ] && kill "$EMERG_PID" 2>/dev/null || true
  [ -n "$FBCP_PID" ] && kill "$FBCP_PID" 2>/dev/null || true
  # polite TV standby on the way out (the old wrapper.py behavior).
  # Deliberately NOT 'vcgencmd display_power 0': on this stack that is
  # a one-way door — the output cannot be brought back without a
  # reboot, so blanking here would strand the next session.
  [ "$TV" = 1 ] && [ -n "${CEC:-1}" ] && { echo 'standby 0' \
    | cec-client -s -d 1 >/dev/null 2>&1 || true; }
  screen -S "$SESSION" -X quit 2>/dev/null || true
}
trap cleanup EXIT

# screen -Dm: a REAL pty session (ES refuses to live without one — the
# wrapper.py lesson) run attached-in-foreground, so this script blocks
# until EmulationStation exits. The moment it does, the trap cleans up
# and the tapbox-extra wrapper takes the box back.
#
# Why not the proven `screen -X stuff 'emulationstation\n'` from
# wrapper.py: stuff is fire-and-forget into a pre-existing session —
# right for a forever-daemon that never needs to know when ES ends,
# wrong here where THIS script's exit IS the give-the-box-back signal.
# -Dm keeps both properties that made stuff work: the real pty, and
# (via `runuser -l`, a full LOGIN environment: HOME/PATH/XDG as the
# user — plain `runuser -u` leaked HOME=/root and ES wrote its config
# to the wrong home) the interactive-shell surroundings.
screen -Dm -S "$SESSION" runuser -l "$RP_USER" -c emulationstation
