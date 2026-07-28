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
set -e
# optional: reclaim the API's RAM for hungry cores (restore restarts it)
systemctl stop tapbox-daemon tapbox-mpris tapbox-bt-reconnect
/usr/local/bin/fbcp-ili9341 &            # mirror /dev/fb0 -> ST7789
FBCP=$!
emulationstation                          # blocks until the user quits
kill $FBCP 2>/dev/null || true
# just exit — the wrapper's ExecStopPost brings TapBox back
```

Install RetroPie the normal way (RetroPie-Setup on top of the same OS)
before wiring the script; none of that touches TapBox.

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
