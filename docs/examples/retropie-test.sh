#!/usr/bin/env bash
# tapbox-name: RetroPie (stuff test)
#
# EXPERIMENT, not the shipping launcher. Same box teardown as
# retropie.sh, but EmulationStation is started the way palchrb's own
# retropie_wrapper did it — the one flow proven to work on this
# hardware:
#
#   retropie.sh (current)   screen -Dm ... runuser -l user -c emulationstation
#                           one command: screen is the parent, blocks
#                           here until ES exits, ES inherits screen's pty
#
#   this script             runuser -l user -c 'screen -dmS retropie'
#                           ...then 'screen -X stuff' types the command
#                           INTO that already-running login shell
#
# The difference under test: in -Dm the pty exists but nothing ever ran
# a login shell in it — ES is exec'd straight into a bare pty. With
# stuff, a real interactive bash owns the pty first and ES is launched
# BY that shell, inheriting everything a login shell sets up. That is
# the classic reason ES behaves differently between the two, and it is
# the one thing we have never varied.
#
# Consequence you already know: this script does NOT know when ES ends
# (stuff is fire-and-forget), so it parks instead of returning the box.
# To come back:  sudo systemctl stop tapbox-extra   (runs the normal
# restore — no reboot needed), or just reboot.
#
# Install:
#   sudo install -m 755 -o root -g root \
#     docs/examples/retropie-test.sh /etc/tapbox/extras/
set -u
RP_USER="${RP_USER:-palchrb}"
SESSION="${SESSION:-retropie}"

echo "retropie-test: starting ES via a pre-existing screen session (stuff)"

# --- same teardown as the real script ---------------------------------
# Free the RAM ES needs; the wrapper's ExecStopPost restarts all of it.
systemctl stop tapbox-bt-reconnect 2>/dev/null || true
systemctl stop tapbox-daemon tapbox-mpris 2>/dev/null || true
systemctl stop tapbox-btsnoop 2>/dev/null || true
sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true

# wifi STAYS UP — the whole point is to watch this from ssh
iw dev wlan0 set power_save on 2>/dev/null || true

# hang up BT audio sinks; HID controllers are left alone
for d in $(bluetoothctl devices Connected 2>/dev/null | awk '{print $2}'); do
  bluetoothctl info "$d" | grep -q "Audio Sink" \
    && bluetoothctl disconnect "$d" >/dev/null
done

# wake the TV and grab its input (CEC negotiates the port itself)
if command -v cec-client >/dev/null; then
  echo 'on 0' | cec-client -s -d 1 >/dev/null 2>&1 || true
  echo 'as'   | cec-client -s -d 1 >/dev/null 2>&1 || true
fi

# --- the actual experiment --------------------------------------------
# A DETACHED login session, owned by RP_USER (so `screen -ls` as that
# user finds it), with a real interactive shell inside it...
runuser -l "$RP_USER" -c "screen -dmS $SESSION" || {
  echo "retropie-test: could not create the screen session" >&2
  exit 1
}
sleep 2   # let the login shell finish its rc files before typing at it

# ...then type the command into that shell, exactly like wrapper.py did.
runuser -l "$RP_USER" -c \
  "screen -S $SESSION -X stuff 'emulationstation\n'" || {
  echo "retropie-test: stuff into the session failed" >&2
  exit 1
}

echo "retropie-test: ES launched into screen session '$SESSION' as $RP_USER"
echo "retropie-test:   watch it:   sudo -u $RP_USER screen -r $SESSION"
echo "retropie-test:   give back:  sudo systemctl stop tapbox-extra"

# Park. stuff is fire-and-forget, so this script cannot know when ES
# ends — exiting here would hand the box back mid-game. Sleeping keeps
# the transient unit alive (and with it the stopped-services state)
# until someone stops it or reboots.
while sleep 3600; do :; done
