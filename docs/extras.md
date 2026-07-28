# Extras — run your own things on the box

TapBox has a deliberately tiny extension hook: the owner can drop
launch scripts on the box, and a hidden button chord starts them from
the screen. Nothing else ships in TapBox — no emulators, no extra
packages, no API. The motivating example is RetroPie, but an extra can
be anything that wants the box's screen, buttons and speaker for a
while.

## How it works

- **Drop a script in `/etc/tapbox/extras/`** (over SSH — this is the
  only way in, by design). It must be a regular file, executable,
  **owned by root, and not group/world-writable** — the screen skips
  anything else, because these scripts run as root.
- Give it a display name with a header comment:

  ```sh
  #!/usr/bin/env bash
  # tapbox-name: RetroPie
  ```

  Without one, the filename (minus extension) is shown.
- **Hold X + Y (the two right-side buttons) for ~2 seconds** — the
  same gesture family as hold-A+B for settings. The Extras menu only
  exists when the directory has entries; on a stock box the chord does
  nothing.
- Pick an entry, confirm with A. TapBox then stops the screen UI, the
  idle auto-power-off, the media-key listener and audio playback (the
  current position is bookmarked), and your script owns the machine.
- **When your script exits — however it exits — TapBox comes back.**
  The extra runs as a transient systemd unit whose `ExecStopPost`
  restores the full TapBox service set even if the script crashes or
  is killed.

## The contract for your script

- You own the SPI display, the four GPIO buttons and the ALSA audio
  device until you exit.
- `tapbox-daemon` (the HTTP API) is left running on purpose: a linked
  phone can still see battery and send `POST /system/shutdown` — the
  remote escape hatch if your program wedges. Stop it from your script
  if you need the ~35 MB of RAM; the restore starts it again.
- You may `systemctl stop` more TapBox services. **Never `disable` or
  `mask` anything** — the return path unmasks-then-starts the standard
  set, but a disabled unit stays broken across the next boot.
- Low-battery power-off (PiSugar) stays active while you run.
- There is no timeout. The physical PiSugar button is the last-resort
  exit.
- **The CPU is unparked for you**: TapBox boots in power-save (the
  governor pinned to powersave = 600 MHz flat). The wrapper lifts it
  to ondemand for the duration and puts the previous mode back on
  return — your script does not need to touch cpufreq. Do NOT
  overclock a battery-powered box in its case; if you must experiment,
  do it on the charger.

## Example: RetroPie launcher

Reality check for this hardware first:

- The Pirate Audio display is **not a Linux framebuffer** — RetroArch
  and EmulationStation render to `/dev/fb0`, so you need an SPI
  mirror such as `fbcp-ili9341` (build it yourself; configure for
  ST7789 240x240, SPI CE0, DC=BCM9, backlight BCM13). TapBox's UI is
  stopped by the wrapper, so nothing fights you for the SPI bus.
- **Four buttons are not a gamepad.** Use a USB controller through the
  micro-USB OTG port.
- Audio goes out the I2S DAC (ALSA card `sndrpihifiberry` /
  `tapbox_local`'s underlying card) — point RetroArch at it.
- 512 MB RAM total: stop `tapbox-daemon` too if a core needs the
  headroom (see below), and stick to 8/16-bit systems.

```sh
#!/usr/bin/env bash
# tapbox-name: RetroPie
#
# Test launcher for RetroPie via the extras hook.
# - NO autostart anywhere: when RetroPie-Setup offers "start
#   EmulationStation at boot", answer NO — boot must stay TapBox, and
#   this script (X+Y chord) is the only way ES starts.
# - ES runs inside a GNU screen session, so from SSH you can watch and
#   debug it live:     screen -x retropie
# - The wrapper has already stopped tapbox-ui/idle/buttons, stopped
#   playback and go-librespot, and lifted the CPU governor — don't
#   redo any of that here.
set -u
RP_USER="${RP_USER:-palchrb}"   # the user RetroPie-Setup installed for

# quiet the BT pager while pairing/using a BT controller (restore
# restarts it). Uncomment the daemon line too if a core needs the RAM —
# but note the phone's remote escape hatch goes away while it is down.
systemctl stop tapbox-bt-reconnect 2>/dev/null || true
# systemctl stop tapbox-daemon tapbox-mpris

# RF quiet for gaming: wifi off (coex + input latency on the shared
# radio) and hang up BT AUDIO sinks — the controller (HID) keeps the
# radio to itself; game sound goes out the jack anyway. NB: wifi off =
# no SSH / screen -x until the session ends — set KEEP_WIFI=1 when you
# need live debugging: wifi then stays up but is SOFTENED instead
# (power-save + 5 dBm), which hands BT most of the airtime. Safe either
# way: the TapBox return trip rfkill-unblocks both radios and resets
# txpower to auto.
if [ "${KEEP_WIFI:-0}" = 1 ]; then
  iw dev wlan0 set power_save on 2>/dev/null || true
  iw dev wlan0 set txpower fixed 500 2>/dev/null || true
else
  rfkill block wifi
fi
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
  echo "retropie-extra: WARNING: $FBCP_BIN missing — no picture on the SPI display"
fi

cleanup() {
  [ -n "$FBCP_PID" ] && kill "$FBCP_PID" 2>/dev/null || true
  screen -S retropie -X quit 2>/dev/null || true
}
trap cleanup EXIT

# screen -Dm runs ATTACHED-IN-FOREGROUND: this script blocks here until
# EmulationStation exits — essential, because the moment this script
# exits, the wrapper takes the box back. runuser drops root: RetroPie's
# configs live in the install user's home.
screen -Dm -S retropie runuser -u "$RP_USER" -- emulationstation
# reaching here = Quit chosen in ES -> trap cleans up -> TapBox returns
```

Install RetroPie the normal way (RetroPie-Setup on top of the same OS)
before wiring the script; none of that touches TapBox. `apt install
screen` if the box lacks it.

## Security posture (why it is shaped this way)

- The extras directory is outside every upload/media root; the PWA's
  media upload whitelists audio/image extensions and cannot write
  here. There is **no HTTP endpoint** to list or start extras — not
  even with the box token. Handing root to arbitrary code is the
  maximal action; it requires SSH to install and hands on the box to
  start.
- The root-owner + not-writable check on each file means a kid (or a
  compromised unprivileged process) cannot plant or modify an entry.
- The X+Y chord plus a confirm screen keeps curious button-mashers
  out; there is nothing to find in the settings menu.
