# pipod

A **real Apple iPod** reborn as a TapBox player: gut a 4th-gen/Photo iPod,
drop in a **Pi Zero 2 W + PiSugar 3**, keep the **original click wheel** as
the controller, fit a small **TFT** with an iPod-style scrolling UI, and
play out a **3.5 mm jack *or* Bluetooth** — running TapBox's existing
Spotify/podcast backend.

Think of pipod as a **TapBox variant**, not a rewrite: the whole backend
(go-librespot fork, mpv, the `tapbox` package, PiSugar tooling in
`../pi/`) is reused unchanged. pipod only adds a wheel reader, an
iPod-style UI, and a Hold-switch daemon. **Nothing in `../pi` is modified.**

Inspired by Guy Dupont's "sPot"
([dupontgu/retro-ipod-spotify-client](https://github.com/dupontgu/retro-ipod-spotify-client)),
but backed by TapBox and with a wired jack + BT + Hold-as-power.

## Status

**Research + scaffold.** The research is done and sourced; the code here is
**untested draft** to iterate on once hardware is in hand.

## Read in this order

1. **[RESEARCH.md](./RESEARCH.md)** — sourced findings: the click-wheel
   protocol, which iPod to buy, fitting the boards, audio-out options, and
   the Hold switch. Start here — §0 is the decision that drives everything.
2. **[HARDWARE.md](./HARDWARE.md)** — BOM, GPIO pin map, wiring, boot config.
3. **[SOFTWARE.md](./SOFTWARE.md)** — what's reused from TapBox vs. built
   new, the install flow, and build milestones.

## The three decisions baked in

- **Real iPod shell + real click wheel.** Buy a **4th-gen click-wheel iPod
  or iPod Photo** for the *proven 8-pin wheel* — **not** an "iPod Classic"
  6G/7G (those are 14-pin, no turnkey driver — RESEARCH §0/§1). With the
  bare-LiPo power path a thin shell is fine; only the PiSugar alt forces a
  thick shell (e.g. Photo 60 GB).
- **Jack *and* BT, switchable.** TapBox's `output.py` already models exactly
  `local` (I²S jack) + `bt` — so this is mostly free (RESEARCH §3, SOFTWARE).
- **TFT + wheel navigation.** New scrolling UI (`src/podui.py`); TapBox's
  4-button `ui.py` is the wrong paradigm but donates its art-cache/marquee.
- **Power: bare LiPo + PowerBoost 1000C + MAX17048 gauge** (primary) — bigger
  battery, thinner fit, latching Hold→`EN` = clean on/off, battery % over
  I²C via a small `src/battery.py` shim. PiSugar 3 is the documented "easy
  mode" alternative (RESEARCH §2).

## Layout

```
pipod/
├── README.md            ← you are here
├── RESEARCH.md          sourced findings (the "why")
├── HARDWARE.md          BOM, pin map, wiring, boot config
├── SOFTWARE.md          reuse map, install flow, milestones
├── clickwheel/
│   ├── click.c          DRAFT wheel reader (pigpio → UDP)
│   └── README.md        build + debug
├── src/
│   ├── podui.py         DRAFT iPod-style UI + wheel router
│   ├── holdswitch.py    DRAFT Hold lock + safe shutdown
│   └── battery.py       DRAFT MAX17048 fuel-gauge reader
└── install-pipod.sh     DRAFT additive installer
```

## Quick start (once you have the parts)

```
sudo ../pi/install.sh            # base TapBox backend (unchanged)
sudo ./install-pipod.sh all      # pipod overlay + wheel/ui/hold services
sudo reboot
# then: PWA → Player → Audio out → Built-in   (routes to the jack)
```

## Biggest unknowns to verify with hardware (RESEARCH closing §)

- LiPo pouch dimensions vs. your shell's internal cavity (or PiSugar stack
  thickness if you pick the alt).
- Your wheel's controller (Synaptics vs Cypress) → decoder bit order.
- Hold-switch pad map (meter it).
