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

# mirror /dev/fb0 onto the Pirate Audio ST7789 (build fbcp-ili9341
# yourself; without it nothing reaches the SPI display)
FBCP_BIN="${FBCP_BIN:-/usr/local/bin/fbcp-ili9341}"
FBCP_PID=""
if [ -x "$FBCP_BIN" ]; then
  "$FBCP_BIN" & FBCP_PID=$!
else
  echo "retropie: WARNING: $FBCP_BIN missing — no picture on the SPI display"
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
  screen -S "$SESSION" -X quit 2>/dev/null || true
}
trap cleanup EXIT

# screen -Dm: a REAL pty session (ES refuses to live without one — the
# wrapper.py lesson) run attached-in-foreground, so this script blocks
# until EmulationStation exits. The moment it does, the trap cleans up
# and the tapbox-extra wrapper takes the box back.
screen -Dm -S "$SESSION" runuser -u "$RP_USER" -- emulationstation
