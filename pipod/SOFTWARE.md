# pipod — software plan

pipod runs the **whole Vibb backend unchanged** and adds three small
front-end pieces for the wheel, the screen, and the Hold switch. Nothing in
`../pi/` is modified — pipod layers on top.

## What comes for free from Vibb

| Capability | Vibb component | pipod change |
|---|---|---|
| Spotify Connect + on-disk cache | go-librespot fork, `spotify.py` | none |
| Podcasts / RSS / local, offline cache, resume | mpv, `content.py`, `library.py`, `radio.py` | none |
| **Audio routing: jack ⇄ BT, switchable** | `output.py` (`local` = I²S, `bt` = speaker) | none — the jack **is** the `local` PCM |
| Media commands (play/pause/next/prev/vol) | `boxapi.py` HTTP on `:3679` | wheel calls the same endpoints |
| PiSugar battery/idle/RTC tooling | `power.sh` | **not used** on the primary LiPo path (PiSugar-specific; no-ops). Battery % comes from `src/battery.py` (MAX17048). Reuse `power.sh` only if you pick the PiSugar alt. |
| BT auto-reconnect | `btwatchd.py` | none |
| Parent PWA (library, wifi, settings) | daemon/PWA | none |

Key insight: **`output.py` already models exactly two outputs, `bt` and
`local`, and `local` is an I²S hifiberry-dac PCM.** pipod's 3.5 mm jack is
that `local` output. So "jack and BT, equally, switchable in the UI" is
*already implemented* — pipod just needs the UI to expose the toggle (the
PWA already does; podui adds it to the wheel UI too).

## The three new components (in `src/` and `clickwheel/`)

### 1. `clickwheel/click.c` — wheel reader (C, pigpio)
Derived from Dupont's `click.c`. Reads Clock (BCM23) + Data (BCM25) via
pigpio DMA edge callbacks at **1 µs sample rate**, decodes the 32-bit
frame, and emits events over **UDP `127.0.0.1:9090`** as 3 bytes:
`[button_idx, button_state, wheel_pos]`. Optional haptic pulse on BCM26.
- Build: `gcc -Wall -pthread -o click click.c -lpigpio -lrt`
- Runs as `pipod-wheel.service` (needs `pigpiod` or root for DMA).
- ⚠️ If you read garbage, flip bit order/edge (Synaptics vs Cypress —
  RESEARCH §1). Starter is written for the Cypress `0x35`-header frame.

### 2. `src/podui.py` — iPod-style UI + input router (Python)
A new front-end because Vibb's `ui.py` is a **4-discrete-button, 240×240**
model — the wrong paradigm for a wheel. podui:
- Listens on UDP `:9090` for wheel events.
- **Scroll → move selection**; accelerates with scroll speed. **Center =
  select**, **Menu = back**, **Play/Pause / Next / Prev** = media (routed to
  `boxapi` exactly like `buttons.py`).
- Menu model: `Now Playing / Music (library) / Podcasts / Settings`, each a
  vertical scrolling list — classic iPod feel.
- Renders to the ST7789 320×240 over SPI.
- **Reuses from `ui.py`:** the album-art disk-cache helpers (`_art_disk*`),
  marquee text, and the `boxapi` polling pattern — copy those, don't reinvent.
- Honors a **lock flag** set by `holdswitch.py` (ignores wheel input while
  Hold is engaged; dims the screen).

### 4. `src/battery.py` — MAX17048 fuel-gauge reader (Python, smbus2)
Reads the LiPo gauge over I²C (0x36) and writes `%`/volts to
`/run/pipod-battery.json`; podui shows it in the status bar. This is the
~30-line shim that stands in for PiSugar's built-in I²C status on the bare
LiPo power path (RESEARCH §2). Runs as `pipod-battery.service`. Omit it if
you chose the PiSugar 3 alt (use `power.sh` instead).

### 3. `src/holdswitch.py` — Hold lock + safe shutdown (Python, gpiozero)
Reads the Hold switch as a **level** on BCM17 (RESEARCH §4):
- Enter Hold → write a lock marker (podui stops acting on the wheel).
- Held in Hold **> N s** → `sudo poweroff` (PiSugar safe-shutdown cuts power).
- Leave Hold → clear lock.
Runs as `pipod-hold.service`.

> Alternative wheel path (not chosen): emit wheel events into `uinput` as
> media keys so the existing `buttons.py` picks them up. That covers
> play/pause/next/prev but **not** scroll-to-navigate, so podui reads UDP
> directly instead. Keep `buttons.py` running too — it still handles BT
> AVRCP buttons and PiSugar taps.

## Install flow (`install-pipod.sh`, additive)

```
# 1. Base Vibb install (unchanged)
sudo ../pi/install.sh

# 2. pipod audio overlay (hifiberry-dac WITHOUT gpio=25; enables SPI)
#    -> writes the config.txt block from HARDWARE.md
sudo ./install-pipod.sh audio

# 3. Build + install the wheel reader, podui, hold daemon as systemd units
sudo ./install-pipod.sh services

# 4. Point audio at the jack (the existing "local" output)
#    PWA -> Player -> Audio out -> Built-in   (or: POST /output {local})

sudo reboot
```

`install-pipod.sh` deliberately **does not** call `vibb-power
hat-audio-on` (that adds `gpio=25=op,dh`, which would fight the wheel's Data
pin). It writes the pipod variant of the overlay instead.

## Milestones (suggested order)

1. **Backend on the bench** — flash Pi, run Vibb install, confirm Spotify
   + a podcast play out a **USB DAC** (proves software before wiring I²S).
2. **I²S jack** — wire PCM5102, `dtoverlay=hifiberry-dac`, switch `output`
   to `local`, confirm the jack plays. BT still switchable.
3. **Wheel reading** — wire the FFC, build `click.c`, watch UDP events with
   a `nc -ul 9090` dump. Nail bit order here.
4. **podui** — scrolling menu on the TFT, wheel drives it, center plays.
5. **Hold switch** — lock + safe shutdown.
6. **Integration** — everything inside the shell; haptics; battery curve
   (`vibb-power curve`), idle shutdown, PiSugar taps as extra transport.

## Files in this folder
```
pipod/
├── README.md            overview + status
├── RESEARCH.md          sourced findings (the "why")
├── HARDWARE.md          BOM, pin map, wiring, boot config
├── SOFTWARE.md          this file
├── clickwheel/
│   ├── click.c          DRAFT wheel reader (pigpio, UDP out)
│   └── README.md        build + debug notes
├── src/
│   ├── podui.py         DRAFT iPod-style UI + wheel router
│   ├── holdswitch.py    DRAFT Hold lock + safe shutdown
│   └── battery.py       DRAFT MAX17048 fuel-gauge reader
└── install-pipod.sh     DRAFT additive installer
```

All code here is a **starting draft to iterate on with hardware in hand** —
untested against a real wheel. The research and pin maps are the reliable
part; the drafts save you the blank-page step.
