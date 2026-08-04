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

# Free the RAM EmulationStation needs. Field 2026-08-04: ES launched,
# grew to ~275 MB resident, and the OOM killer took it 28s in — this
# box has ~414 MB usable and swap was already exhausted. Everything
# stopped here is restarted by the wrapper's ExecStopPost, so the box
# always comes back whole:
#   bt-reconnect  quiets the BT pager while a controller is in use
#   daemon+mpris  the orchestration daemon and its AVRCP bridge. NOTE:
#                 tapboxd is also the phone's remote escape hatch, so
#                 while a game runs there is no PWA — the MODE-hold
#                 emergency exit and SSH remain.
#   btsnoop       the BT-crash capture ring writes to /run, i.e. RAM
#                 (three segments). Only running if you enabled it for
#                 a crash hunt; 'stop' does not disable it, so it comes
#                 back on the next boot.
systemctl stop tapbox-bt-reconnect 2>/dev/null || true
systemctl stop tapbox-daemon tapbox-mpris 2>/dev/null || true
systemctl stop tapbox-btsnoop 2>/dev/null || true
sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true

# RF: wifi STAYS UP by default (power save on — latency, not range).
#
# It used to be rfkill'd for the controller's sake, but that is a bad
# default for a box you are still learning to run: the menu launch
# (X+Y) cannot pass KEEP_WIFI, so every session from the screen killed
# the network. Worse, it does not LOOK like that — 'rfkill block' does
# not tear down TCP, so live ssh sessions freeze mid-keystroke without
# disconnecting and only wake when the session ends (field
# 2026-08-04: an evening spent suspecting the screen session). No
# remote log reading, no PWA, no way to see what a failing game says.
#
# Set RF_QUIET=1 to get the old behaviour when a controller genuinely
# needs the radio to itself. The wrapper's restore unblocks either way.
if [ "${RF_QUIET:-0}" = 1 ]; then
  rfkill block wifi
else
  # power save is cheap coexistence help: it costs latency, not range.
  iw dev wlan0 set power_save on 2>/dev/null || true
  # txpower is NOT softened by default any more. 'fixed 500' (5 dBm,
  # against a 20-31 dBm normal) made the link so weak that ssh stalled
  # mid-session — field 2026-08-04 — which defeats the only reason to
  # keep wifi up. Set WIFI_TXPOWER=<mBm> to opt back in (e.g. 1500 =
  # 15 dBm for a milder softening); the wrapper's restore always puts
  # txpower back to auto either way.
  [ -n "${WIFI_TXPOWER:-}" ] \
    && { iw dev wlan0 set txpower fixed "$WIFI_TXPOWER" 2>/dev/null || true; }
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
  python3 - "$SESSION" "$RP_USER" <<'PYEOF' &
import subprocess, sys, threading, time
from evdev import list_devices, InputDevice, ecodes
SESSION, RP_USER, HOLD = sys.argv[1], sys.argv[2], 3
def ctrl_c():
    # the screen session is RP_USER's (created via runuser), so the
    # rescue Ctrl-C has to be sent as that user or it finds no socket
    subprocess.run(["runuser", "-l", RP_USER, "-c",
                    "screen -S %s -X stuff $'\\003'" % SESSION])
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
  # the session belongs to RP_USER now (created via runuser), so the
  # quit has to run as that user or it will not find the socket
  runuser -l "$RP_USER" -c "screen -S $SESSION -X quit" 2>/dev/null || true
}
trap cleanup EXIT

# Launch: a DETACHED login session first, then type the command into
# the interactive shell that owns it.
#
# We used to do `screen -Dm -S x runuser -l user -c emulationstation`
# — one command, and it BLOCKED here, which made the return trip
# trivial. It also did not work: games launched from the menu dropped
# straight back to it, and runcommand.log stayed empty because the
# failure happened before anything could log (field 2026-08-04, proven
# by running the two shapes side by side). The pty existed, but no
# login shell had ever run IN it: ES was exec'd into a bare pty and
# inherited none of what an interactive shell sets up around itself.
# palchrb's original retropie_wrapper had this right from the start.
#
# The cost is that `stuff` is fire-and-forget, so the give-the-box-back
# signal has to be recovered by watching for the ES process instead —
# see the wait loop below.
runuser -l "$RP_USER" -c "screen -dmS $SESSION" || {
  echo "retropie: could not create the screen session" >&2
  echo "RetroPie: could not start — see journalctl -u tapbox-extra" \
    > "${TAPBOX_RUN:-/run}/tapbox-extra.msg" 2>/dev/null || true
  exit 1
}
sleep 2   # let the login shell finish its rc files before typing at it
runuser -l "$RP_USER" -c \
  "screen -S $SESSION -X stuff 'emulationstation\n'" || {
  echo "retropie: stuff into the session failed" >&2
  exit 1
}

# Wait out the session. ES quitting is the give-the-box-back signal,
# but the screen session SURVIVES it (the login shell just returns to
# its prompt), so the ES PROCESS is what we watch.
es_running() { pgrep -u "$RP_USER" -f emulationstation >/dev/null 2>&1; }

# wait for it to appear at all (bounded — a launch that never happens
# must not park the box forever)
for _ in $(seq 1 30); do
  es_running && break
  sleep 1
done
if ! es_running; then
  echo "retropie: EmulationStation never started — handing the box back" >&2
  echo "RetroPie: did not start — see journalctl -u tapbox-extra" \
    > "${TAPBOX_RUN:-/run}/tapbox-extra.msg" 2>/dev/null || true
  exit 1
fi

# Then watch it. 2s polling costs a /proc scan every other second —
# a few ms, invisible next to an emulator — and the exit still has to
# be CONFIRMED before we act: RetroPie's own restart-ES loop relaunches
# within a second or two, and a restart from the ES menu must not be
# mistaken for quitting. Worst case ~6s from quit to the box coming
# back (it was ~15s with a 4-strikes-at-3s counter).
#
# The confirmation window, not the poll, is what costs the seconds, so
# blocking on procps' pidwait instead would buy ~2s for a dependency
# and a second code path. Not worth it.
while :; do
  while es_running; do sleep 2; done
  sleep 4                     # the restart window
  es_running || break
done
echo "retropie: EmulationStation is gone — handing the box back"
