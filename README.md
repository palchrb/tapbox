# tapbox

> **Working name** — final brand TBD. Rename the directory and update references when decided.

An open, RFID-controlled portable speaker that plays music from your existing streaming subscriptions (Spotify, Tidal, podcasts, local files) instead of locking you into a proprietary content store.

Anti-lock-in alternative to Tonies / Yoto / Toniebox for tech-conscious parents.

## Status

**Working test rig** on a Pi Zero 2 W + PiSugar 3 + BT speaker: Spotify
(Connect, via a go-librespot fork with on-disk audio cache), NRK/RSS
podcasts with offline episode cache and exact resume, parent PWA
(library, wifi, BT, settings), screen UI for the Pirate Audio HAT
(dev-mode complete), boot resume, battery tooling. RFID hardware on
order; card slot flow implemented behind it.

- See [SPEC.md](./SPEC.md) for the platform spec, plus
  [SPEC-A-explorer.md](./SPEC-A-explorer.md) (screen navigator) and
  [SPEC-B-card-player.md](./SPEC-B-card-player.md) (card player).

## Installation manual

Target hardware: Raspberry Pi Zero 2 W, optional PiSugar 3 battery,
a Bluetooth speaker (or the Pimoroni Pirate Audio Speaker HAT), and a
Spotify **Premium** account (family member accounts work). Assumed
already done: Raspberry Pi OS **(Trixie-based; Bookworm also works)** flashed with the Raspberry
Pi Imager (set hostname, user, Wi-Fi and SSH there), and — if you want
remote admin — Tailscale installed and logged in.

### 1. Install TapBox

SSH in, clone, run the installer:

```
git clone <this repo> ~/tunebox
cd ~/tunebox
sudo ./pi/install.sh
```

The first run asks one question — a **name for the box** (lowercase
letters/digits/hyphens). It becomes both the mDNS hostname
(`<name>.local`) and the Spotify Connect device name
("TapBox (<name>)"). Non-interactive installs: `TAPBOX_NAME=<name>
sudo ./pi/install.sh`.

The script is idempotent and does everything on-box: apt packages
(bluez-alsa, mpv, ffmpeg, avahi, ...), the go-librespot fork (Spotify
Connect daemon with on-disk audio cache, pinned version), ALSA
routing, the `tapbox` Python package, all systemd services
(orchestration daemon + PWA on `:3679`, BT auto-reconnect, media
buttons, captive-portal DNS), and enables I2C/SPI for the hardware
that arrives later. Services for hardware you don't have yet
(`tapbox-ui` for the screen, `tapbox-rfid` for the card reader) are
installed **disabled**.

### 2. Log in to Spotify

The installer's last step waits for this, and you can do it any time:

1. Open the Spotify app on your phone — **same Wi-Fi as the box**.
2. Play any song, tap the devices icon.
3. Pick **"TapBox (<name>)"**.

The login is stored on the box and survives reboots. To hand the box
to another account later: PWA → Settings → Spotify → *Switch account*.

### 3. Pair the Bluetooth speaker

Open the parent PWA at `http://<name>.local:3679` (add it to your
phone's home screen — it has a proper icon). Under **Settings →
Bluetooth speaker**:

- Put the speaker in pairing mode, then use **Pair nearest**
  (one-button flow), or **Scan for new** and pick it from the list.
- The chosen speaker becomes the *configured* output: audio and
  auto-reconnect follow it. Pairing more speakers is fine — tap
  *Connect* on one to make it the active output.

From now on, turning the speaker on is enough; the box reconnects by
itself within seconds (btwatchd listens for BlueZ D-Bus events; set
`TAPBOX_BT_BACKEND=cli` on the tapbox-bt-reconnect service to fall
back to the old 60s poll loop). Pairing itself still runs through
bluetoothctl by default; set `TAPBOX_BT_PAIR=dbus` on the
tapbox-daemon service to use the new Agent1 path (kept opt-in until
the rig matrix in PLAN-bt-b2-pairing.md passes).

**Pairing from a car / head unit:** cars drive the pairing themselves
and expect the *device* to be visible. PWA → Bluetooth → **Pair from
car**: the box becomes discoverable for ~2 minutes — start the pairing
from the car's Bluetooth menu, then pick the car as speaker when it
appears in the device list. The box only accepts pairings during that
window (never silently), and pairing alone never moves the audio — the
new device is just listed, one tap away from becoming the output.

### 4. Build the library

PWA → **Library** → *Add*: paste a Spotify link (playlist/album/
artist/audiobook), an NRK series/podcast link, any RSS feed, or a
local folder path. Per entry you choose the play order and an
**offline cache** depth (keep newest N episodes on the SD card —
they play without internet). Podcast/RSS episodes cache as plain
files under `/var/lib/tapbox/cache`; Spotify caches encrypted audio
(faster + cheaper on repeat plays, but needs a live session).

### 5. Battery (PiSugar 3) — optional

1. Install pisugar-server with PiSugar's own installer (their curl
   script; it's interactive, which is why install.sh doesn't do it).
2. Re-run `sudo ./pi/install.sh` — it now applies the TapBox-measured
   battery curve (percent ≈ remaining playtime) automatically.
3. In the PiSugar web UI (`http://<name>.local:8421`): enable **safe
   shutdown** at ~5%.
4. Optional extras: `sudo tapbox-power taps-on` (PiSugar button:
   short=play/pause, double=next, long=prev), `tapbox-power log-on`
   (battery CSV logger), `tapbox-power curve` (recalibrate).

### 6. Power tuning — optional

- Power save at boot (powersave governor, LEDs/HDMI off, Wi-Fi power
  save) is applied automatically — install.sh runs `tapbox-power
  boot-on`. Undo per-box with `sudo tapbox-power boot-off`; back to
  full speed anytime with `sudo tapbox-power perf`.
- Settings in the PWA: auto-off when idle, Wi-Fi auto-off away from
  known networks, screen timeout, volume cap, resume on power-on.
- Squeezing the last drops: add `maxcpus=2` to
  `/boot/firmware/cmdline.txt` by hand (the RPi OS kernel has no CPU
  hotplug, so `tapbox-power save` can't park cores at runtime).
  Measured on zero2: idle cores sleep deeply anyway — the saving is a
  few mA at best, so most boxes should skip this.

### 7. When the hardware arrives

**Pirate Audio Speaker HAT (screen + built-in speaker):**

```
sudo tapbox-power hat-audio-on    # dtoverlay=hifiberry-dac + amp enable
sudo reboot
# then: PWA -> Player -> Audio out -> Built-in
sudo systemctl enable --now tapbox-ui
```

**PN532 RFID reader (card slot):**

```
sudo systemctl enable --now tapbox-rfid
# slot-mode switch and options: /etc/tapbox/rfid.conf
# map a card: sudo tapbox-card map <link>   then tap/insert the card
```

### 8. Moving the box to a new Wi-Fi

A box that finds no known network (and has none saved) starts its own
setup hotspot **TapBox-<name>** (password `tapbox123`); joining it
pops a captive portal straight into the PWA, where you pick the new
network. A box with saved networks: use the PWA's Wi-Fi card while
it's still on the old network, or the screen's settings.

### Updating

```
cd ~/tunebox && git pull && sudo ./pi/install.sh
```

The script restarts only the services whose files changed; playback
position survives (bookmark + resume).

### Kept outside install.sh (by design)

| What | How | Why manual |
|---|---|---|
| OS basics | Raspberry Pi Imager (hostname, user, wifi, SSH) | pre-boot; cloud-init is disabled after first boot (install.sh) |
| pisugar-server | PiSugar's own installer script, then re-run install.sh | third-party interactive installer; install.sh patches its config (battery curve, RTC units, log quieting) when present |
| PiSugar safe-shutdown / taps | PiSugar web UI (:8421) or `tapbox-power taps-on` | user preference |
| Pirate Audio HAT (when mounted) | `sudo tapbox-power hat-audio-on` + reboot + enable `tapbox-ui` | hardware-gated |
| PN532 RFID (when wired) | `sudo systemctl enable --now tapbox-rfid` | hardware-gated |
| Spotify login | pick the box under Devices in the Spotify app (same wifi) | zeroconf by design; install.sh waits for it in step 8/8 |
| BT speaker pairing | PWA settings -> Bluetooth (or screen) | per-home config |

(Power-save at boot, `boot_delay=0` and disabling cloud-init moved INTO
install.sh 2026-07-18 — a reflash reproduces them. The battery CSV logger
stays opt-in: `tapbox-power log-on` only while calibrating.)

## Why this exists

Today's kid-friendly speakers (Tonies, Yoto) sell hardware cheaply and recoup margin via proprietary content figurines/cards. Parents end up paying twice for content they already own through Spotify/Apple Music/etc.

tapbox flips that: bring your own music subscriptions, control playback with reusable RFID/NFC cards, and own the device end-to-end.

## License

TBD. Likely Apache 2.0 (aligns with the Music Assistant dependency) or dual-licensed for commercial distribution. Not yet committed.
