# pipod — research: turning a real iPod into a TapBox-based player

*Compiled 2026-07-21. This is the sourced research behind pipod. Design
decisions distilled from it live in [HARDWARE.md](./HARDWARE.md) and
[SOFTWARE.md](./SOFTWARE.md); this file is the "why", with citations.*

pipod is a **variant of TapBox**: same software core (go-librespot fork,
mpv, the `tapbox` Python package, PiSugar 3 tooling), but the enclosure is
a **real Apple iPod**, the input is the **original click wheel**, the
screen is a small TFT with an **iPod-style scrolling UI**, and audio goes
out a **3.5 mm jack (line/headphone) *and* Bluetooth**, switchable in the
UI exactly like TapBox does today.

The reference build for the concept is Guy Dupont's viral "sPot"
(dupontgu/retro-ipod-spotify-client). pipod differs from sPot in three
deliberate ways: it reuses TapBox's mature backend instead of sPot's
Spotipy/Web-API front-end, it keeps both a wired jack **and** BT, and it
wires the Hold switch to power/lock.

---

## 0. The one decision that drives everything: which iPod to buy

Two hard constraints pull in opposite directions:

- **The click wheel you can actually read** is the **4th-generation era
  8-pin wheel** (monochrome 4G *and* iPod Photo/Color). Every working
  maker project reads this wheel. The **iPod Video 5G / 5.5G and the
  Classic 6G/7G use a different 14-pin wheel** with the click switches
  moved to the mainboard and a different controller — **no turnkey driver
  exists** for it. The iPod **mini** uses yet another Molex ribbon.
- **PiSugar 3 needs depth.** A Pi Zero 2 W + PiSugar 3 sandwich is
  ~11–13 mm; the **thin** shells (~10.5 mm: 4G mono, Video 30 GB, Classic
  thin/7G) can't take it. You need a **thick** shell (~13.5–14 mm).

**The shell that satisfies both: an iPod Photo / 4th-gen Color in a thick
capacity (e.g. 60 GB).** It carries the proven 8-pin wheel *and* has the
depth for PiSugar 3. This is the single most important sourcing decision —
buy the wheel generation first, not the prettiest shell.

If you fall in love with a 5G Video/Classic shell (also thick enough at
14 mm), budget real reverse-engineering time for its 14-pin wheel, or plan
to substitute a rotary-encoder replica behind the wheel (loses the
capacitive-scroll feel). See §1.

> **Terminology trap:** the **2004 4th-gen click-wheel iPod** and the
> **iPod Photo (4th-gen color)** carry the proven **8-pin** wheel — *these*
> are the target. The branded **"iPod Classic" (6G/7G, 2007+)** is a
> **different, 14-pin** wheel with no turnkey driver. "4th gen" ≠ "Classic".

### Power is a choice, not fixed to PiSugar (see §2 for the trade-off)
PiSugar 3 is the easy, integrated option **but not required** — and for a
thin-ish 4th-gen shell a **bare LiPo + a boost/charger board (Adafruit
PowerBoost 1000C)** is the *proven* path (it's what Dupont's sPot used) and
usually fits better with a bigger battery. Full comparison in §2.

Sources: dupontgu build https://github.com/dupontgu/retro-ipod-spotify-client ·
5th-gen wheel is different https://hackaday.io/project/187907-ipod/log/237444-click-wheel-reverse-engineering-cont ·
iFixit 5G wheel https://www.ifixit.com/Guide/iPod+5th+Generation+(Video)+Click+Wheel++Replacement/614 ·
mini Molex ribbon https://www.idemigods.com/iPod_Mini_Click_Wheel_Cable_Molex_Ribbon_2nd_Gen_p/2g_mlxcblmn.htm

---

## 1. The click wheel — electrical interface & how to read it

### Protocol
The wheel is **SPI-like, not real I²C** (the "it's I²C-ish" myth comes from
the wheel's onboard Cypress PSoC being an I²C-capable chip; the decoded
wire framing is SPI). The wheel acts as **SPI master** and sends **32-bit
(4-byte) packets, one per event**.
Source: https://github.com/Gigahawk/clickwheel_reverse_eng

- Clock ≈ **55 kHz**, 32 cycles/packet, ~2.9 µs start pulse, ~9 µs normal
  pulses, ~11 µs final pulse.
- **SCK**: push-pull while sending, hi-Z idle (host holds high).
- **DATA (wheel→host, "MOSI")**: open-drain, ~100 kΩ pull-up.
- All signalling is **3.3 V** — direct to Pi GPIO, **no level shifting**.
Source: https://jasongarr.wordpress.com/project-pages/ipod-clickwheel-hack/

### 8-pin FFC pinout (4th-gen / Photo), 0.5 mm pitch
1. VBat (power) · 2. SCK (clock) · 3. CFG1 (mode select) · 4. BTN1
(center/menu pulse) · 5. unknown (low) · 6. MOSI (data, open-drain) ·
7. MISO (host→wheel) · 8. GND.
**In practice you only wire four: 3.3 V, GND, Clock, Data.** The rest are
config/enable tied to fixed levels.
Sources: https://github.com/Gigahawk/clickwheel_reverse_eng · https://hackaday.io/project/177034-spot-spotify-in-a-4th-gen-ipod-2004/details

### Packet framing (Cypress CY8C21434 wheel, 32 bits)
- Byte 1: header, always `0x35`
- Byte 2: button bitfield (Menu, Play/Pause, Prev, Next, Center)
- Byte 3: wheel position `0x00`→`0xBE` = **96 positions** around the ring
- Byte 4: touch flag (`0x00` no finger / `0x80` finger present)
Source: https://github.com/Gigahawk/clickwheel_reverse_eng

**Two controller lineages — do not assume one bit order.** Apple
alternated suppliers: **Synaptics T1005** (older; `0x1a`-delimited,
MSB-first) vs **Cypress CY8C21434** (some 4G; CPOL=1/CPHA=0, **LSB-first**,
read on falling edge). If your decoder gets garbage, you likely have the
other chip — swap bit order/edge.
Sources: https://hackaday.io/project/187907-ipod/log/237444-click-wheel-reverse-engineering-cont · https://www.macrumors.com/2005/12/05/ipod-clickwheels-from-synaptics-again/

### How to read it on the Pi — proven path (no MCU)
Dupont's `click.c` reads the wheel **directly on Pi GPIO** using the
**`pigpio`** library's DMA-sampled edge callbacks (not software
bit-banging). Pins (BCM): **Clock = 23, Data = 25, Haptic = 26**. It
decodes the 32-bit frame and forwards events to the UI over **UDP
localhost:9090** (3 bytes: button idx, state, wheel pos). Build:
`gcc -Wall -pthread -o click click.c -lpigpio -lrt`.
Source: https://github.com/dupontgu/retro-ipod-spotify-client/blob/master/clickwheel/click.c

**Why direct GPIO works despite ~9 µs pulses on a non-RTOS:** `pigpio`
uses a **DMA GPIO sampler** (default 5 µs, settable to **1 µs**) with
hardware-timestamped edges. Set the sample rate to 1 µs to catch the
single-digit-µs pulses reliably. Naive user-space bit-banging is *not*
reliable (kernel IRQ latency ~9–11 µs + jitter).
Sources: https://forums.raspberrypi.com/viewtopic.php?t=269035 · https://jjackowski.wordpress.com/2013/03/01/raspberry-pi-linux-interrupt-latency-10%CE%BCs/

### When to add an MCU (Path B, optional)
An RP2040/Pico reading the wheel via **PIO or SPI-slave** and presenting to
the Pi as **USB-HID** gives bulletproof timing and frees the Pi's CPU — but
**no one has published this exact wheel→Pi bridge**, so you'd integrate
proven parts yourself (Garr/Gigahawk decode + TinyUSB HID). Only reach for
it if Path A shows jitter.
Sources: https://github.com/Gigahawk/clickwheel_sample_firmware · https://github.com/Noltari/pico-uart-bridge

### Reference lineage (for further reading)
- **Jason Garr** (~2010, foundational T1005 decode): https://jasongarr.wordpress.com/project-pages/ipod-clickwheel-hack/
- **Gigahawk** (rigorous modern docs + KiCad breakouts): https://github.com/Gigahawk/clickwheel_reverse_eng
- **Guy Dupont "sPot"** (Pi Zero W, direct GPIO): https://github.com/dupontgu/retro-ipod-spotify-client
- **landonr** (ESP32/ESPHome wheel remote): https://github.com/landonr/esphome-remote
- **daniel5151/clicky** (software wheel emulator, protocol ref): https://github.com/daniel5151/clicky

> Names "David Chen / bfmls / Ryan Grassel / ipodwheel" that circulate for
> this could **not** be verified — treat as misattributions of the
> Garr/Gigahawk/Dupont lineage above.

### No commercial shortcut
There is **no** buyable breakout that takes a genuine wheel's FFC and gives
you I²C/USB. The only wheel-native hardware is Gigahawk's self-fab KiCad
board. "iPod-wheel-to-I²C adapter" products don't exist (the wheel isn't
I²C). If you ever abandon the real wheel, you'd rebuild a capacitive ring
from MPR121/CAP1203/CY8CMBR3xxx and write the position logic yourself.
Sources: https://github.com/Gigahawk/clickwheel_breakout_5th_gen · https://www.adafruit.com/product/1982

---

## 2. Fitting Pi Zero 2 W + PiSugar 3 in the shell

### External dimensions (Apple doesn't publish internal cavity dims)
| Model | H×W×D (mm) | Fits PiSugar 3? |
|---|---|---|
| iPod Photo / 4G Color (thick, 60 GB) | ~104×61×~15* | **Yes — and 8-pin wheel** ✅ |
| iPod Classic 6G 160 GB "thick" (A1238) | 103.5×61.8×**13.5** | Yes, but **14-pin wheel** |
| iPod Video 5.5G 80 GB "thick" (A1136) | 104×61×**14** | Yes, but **14-pin wheel** |
| iPod Classic 6G 80 GB / 7G "thin" | 103.5×61.8×**10.5** | No (too thin) |
| iPod Video 5G 30 GB "thin" | 104×61×**10.9** | No |
| iPod mini (A1051) | 91×51×**13** | Tight; different wheel ribbon |

*Photo 60 GB depth is community-reported, not an Apple spec — verify the
one you buy. Sources: EveryMac Classic 6G https://everymac.com/systems/apple/ipod/specs/ipod-classic-6th-generation-specs.html ·
Video 5G https://everymac.com/systems/apple/ipod/specs/ipod_5thgen.html ·
mini https://everymac.com/systems/apple/ipod/specs/ipod_mini.html

### The boards
- **Pi Zero 2 W: 65×30 mm, ~5.2 mm z-height** (bare PCB ~1 mm). z-height
  is from an image-based product brief — **medium confidence**.
  https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/
- **PiSugar 3: 65×30 mm, 1200 mAh, stacks under the Pi on rear pogo pins**
  (doesn't occupy the GPIO header — good, the header is free for the wheel,
  DAC, and TFT). https://docs.pisugar.com/docs/product-wiki/battery/pisugar3/pisugar-3-series

### ⚠️ The missing number
**PiSugar 3 total stack thickness is not documented anywhere** (not
pisugar.com, docs, Tindie, or GitHub). Community estimates the Pi+PiSugar
sandwich at **~11–13 mm**. This is the biggest unknown in the fit. Measure
a real stack before committing to a shell. The face (103×62) is never the
problem — **depth is the only binding constraint.**

### Power: PiSugar 3 vs. bare LiPo + PowerBoost (pick per shell)

| | **PiSugar 3** | **Bare LiPo + PowerBoost 1000C** |
|---|---|---|
| Fit | ~11–13 mm stacked ⚠️ (needs a thick shell) | shape a flat pouch to the cavity — thinner |
| Capacity | fixed 1200 mAh (~5 h, TapBox-measured) | your choice; 2000–2500 mAh ≈ 8–10 h |
| Fuel gauge | yes, over I²C | add a MAX17048 breakout if you want % |
| RTC | yes (DS3231) | none (Zero has no RTC; NTP on wifi) |
| Soft power / safe-shutdown | built in (momentary button, `safe_shutdown_level`) | you wire it (see below) |
| TapBox tooling | **`power.sh` already supports it** (curve, taps, idle, RTC) | `power.sh` no-ops gracefully without pisugar-server |
| Assembly | pogo-pin stack, no soldering | solder LiPo + boost + charge |
| Hold switch as power | ⚠️ latching fits PiSugar's *momentary* pad poorly (§4) | ✅ latching → PowerBoost **EN** pin = clean hardware on/off |

**Recommendation:** for a real 4th-gen shell, **bare flat LiPo (2000–2500 mAh)
+ PowerBoost 1000C** is the better primary — bigger battery, thinner, and
the latching Hold switch becomes a natural power switch on the boost's `EN`
pin (§4). Keep **PiSugar 3** as the "easy mode" alternative when you want the
integrated fuel-gauge/RTC/safe-shutdown and TapBox's `power.sh` niceties, and
you've confirmed a thick shell swallows the stack.
Dupont's sPot used the LiPo+PowerBoost route (1000 mAh):
https://hackaday.io/project/177034/components ·
PowerBoost 1000C (EN pin cuts the boost output):
https://learn.adafruit.com/adafruit-powerboost-1000c-load-share-usb-charge-boost ·
MAX17048 fuel gauge: https://www.adafruit.com/product/5580

### The face is easy — depth is the fight. What real builds do
Every high-fidelity build **reuses the real Apple shell and the real
wheel**, 3D-printing only *internal brackets*:
- **Dupont "sPot":** real 2004 4G case, **Pi Zero W + 2″ Adafruit composite
  TFT (#911) + 1000 mAh + haptic**, printed only an internal base plate.
  https://www.raspberrypi.com/news/raspberry-pi-zero-w-turns-ipod-classic-into-spotify-music-player/ · https://hackaday.io/project/177034-spot-spotify-in-a-4th-gen-ipod-2004
- **syproduction/ipodrpi:** Pi Zero W + 2″ Waveshare 320×240 inside a real
  housing, real wheel. https://github.com/syproduction/ipodrpi
- **RSFlightronics "SpotifyPod":** Dupont clone, real 4G case + wheel via
  FFC, 2″ ST7789V, 750 mAh; publishes internal brackets on Thingiverse.
  https://rsflightronics.com/spotifypod

**Gap:** no popular purpose-built *printable* "iPod shell sized for Pi Zero
+ real wheel + 2–2.4″ display" exists — the curved wheel geometry is hard
to reprint, which is exactly why serious builds reuse real shells. Printed
routes tend to substitute a rotary-encoder replica for the wheel.

---

## 3. Audio out — 3.5 mm jack (+ keep Bluetooth)

**Baseline:** the Pi Zero / Zero 2 W has **no analog audio** — only
mini-HDMI + micro-USB. The solder test points are composite *video*, not
audio. So the jack must come from a DAC.
https://learn.adafruit.com/introducing-the-raspberry-pi-zero/audio-outputs

> **Note on the reference build:** Dupont's sPot did **not** use the mini
> jack — it was **Bluetooth-only**, and the iPod's original headphone jack
> was left **decorative/non-functional**. He explicitly wanted to add a
> **USB DAC** later ("if I switch to a smaller boost module, I can fit a
> small USB DAC and hook it back up"). pipod does what he planned, but via
> an **I²S DAC** (fits better, doesn't consume the one micro-USB OTG port).
> https://hackaday.io/project/177034-spot-spotify-in-a-4th-gen-ipod-2004/details · https://www.whathifi.com/news/this-2004-ipod-has-wi-fi-bluetooth-and-can-stream-any-song-directly-from-spotify

### Three routes
- **(a) USB DAC dongle** — class-compliant, includes a real headphone amp,
  zero config. But it eats the single micro-USB OTG data port. Fine as a
  fallback, awkward inside an iPod (this was Dupont's own fallback idea).
- **(b) I²S DAC (recommended)** — digital over the GPIO header; best
  quality; needs a dtoverlay. **This is what TapBox already uses.**
- **(c) PWM + RC filter** — cheapest, noisy, needs an amp. Not worth it
  here. (For reference: `dtoverlay=pwm-2chan,pin=18,func=2,pin2=13,func2=4`
  + 270 Ω/33 nF + 10 µF/150 Ω per channel.)
  https://learn.adafruit.com/adding-basic-audio-ouput-to-raspberry-pi-zero/pi-zero-pwm-audio

### I²S pin map (BCM → physical)
- **BCM18 = BCLK → pin 12** · **BCM19 = LRCLK → pin 35** ·
  **BCM21 = DATA/DOUT → pin 40**. (BCM20 is I²S *input* only — not used.)
  https://pinout.xyz/pinout/pcm

### ⚠️ A 3.5 mm jack ≠ a headphone amp
Most small I²S boards are **line-level only** and drive 16–32 Ω earbuds
weakly/distorted:
- **MAX98357 — wrong chip.** Mono Class-D **speaker** amp (PWM on
  bridge-tied outputs, no ground ref). Unsuitable for a stereo jack.
  https://learn.adafruit.com/adafruit-max98357-i2s-class-d-mono-amp/overview
- **PCM5102A / GY-PCM5102 — stereo line-out, no HP amp.** Wire
  `VIN=3.3V, GND, BCK=GPIO18, LCK=GPIO19, DIN=GPIO21, SCK→GND` (grounding
  SCK forces the internal PLL — the Pi gives no MCLK).
  https://blog.himbeer.me/2018/12/27/how-to-connect-a-pcm5102-i2s-dac-to-your-raspberry-pi/
- **Adafruit UDA1334A — stereo line-out, no HP amp** (~3 kΩ load; distorts
  into 32 Ω). https://learn.adafruit.com/adafruit-i2s-stereo-decoder-uda1334a

### Board with a REAL headphone amp
- **Pimoroni Pirate Audio: Headphone Amp** — **PCM5100A DAC + PAM8908 HP
  amp**, real 3.5 mm jack, 24-bit/192 kHz, pHAT-sized. Overlay:
  **`dtoverlay=hifiberry-dac`** (+ `gpio=25=op,dh` DAC-enable) — **the exact
  overlay TapBox already ships.** But it's a full pHAT with its own ST7789
  LCD + 4 buttons on BCM 5/6/16/24 and **DAC-enable on BCM25 — which
  collides with the click wheel's Data pin (BCM25).**
  https://shop.pimoroni.com/en-us/products/pirate-audio-headphone-amp
- **HiFiBerry DAC+/DAC2 Pro** — PCM5122 + TPA6133 HP amp, 3.5 mm + RCA, but
  HAT-sized (too big for inside an iPod). Overlay `dtoverlay=hifiberry-dacplus`.

### Recommendation for pipod (resolves the BCM25 clash)
Use a **bare GY-PCM5102A module with its XSMT (mute) pin tied high in
hardware** → then you can **drop `gpio=25=op,dh`**, freeing **BCM25 for the
wheel's Data line** and matching Dupont's `click.c` unmodified. For real
headphone drive add a **small analog HP amp (PAM8908 / TPA6132A2 breakout)**
after the DAC's line-out. Software-wise this is still just
`dtoverlay=hifiberry-dac` — so TapBox's existing `output.py` "local" PCM
path works **as-is**, and BT stays the other selectable output.
TapBox already standardizes on this overlay:
https://github.com/pimoroni/pirate-audio

---

## 4. The Hold switch → power / lock

### What it is
A **latching SPDT slide switch**: COM + two throws (only one connected at a
time). No model-specific pad map is published — **meter the three pads**
(COM has continuity to one throw in each position).
https://www.ifixit.com/Guide/iPod+5th+Generation+(Video)+Headphone+Jack+&+Hold+Switch+Replacement/604

### Why the "obvious" hardware paths fit poorly
- **PiSugar 3 power pads** (Custom = pos.9, Power = pos.10) trigger by a
  **momentary short to BAT+ (not GND)**. A *latching* slide held "on" keeps
  the pad shorted — it doesn't emulate the momentary tap/hold PiSugar
  expects. Power-off is always a long-press.
  https://docs.pisugar.com/docs/product-wiki/battery/pisugar3/pisugar-3-series · https://github.com/PiSugar/PiSugar/wiki/PiSugar-3-Series
- **`dtoverlay=gpio-shutdown`** is **edge-triggered** (default `gpio_pin=3`,
  `active_low=1`, `gpio_pull=up`; BCM3/pin 5 also *wakes* the Pi from halt).
  A latching switch fires one edge on entering Hold, then holds the pin low
  — conflicting with the GPIO3-low wake. Also BCM3 is PiSugar's I²C SCL
  here, so it's taken.
  https://raw.githubusercontent.com/raspberrypi/firmware/master/boot/overlays/README

### The latching switch fits PowerBoost's EN pin perfectly
If you use the **LiPo + PowerBoost 1000C** power path (§2), the latching
Hold switch stops being a mismatch: PowerBoost's **`EN`** pin **disables the
boost output when pulled to GND**, so a slide switch between `EN` and `GND`
is a true hardware on/off — exactly what a latching switch wants. Caveat:
that's a *hard* cut (SD-corruption risk mid-write). Cleanest combo: wire the
switch to a **GPIO for a software safe-shutdown first**, and only use the
EN-cut as the final power-off (or let the Pi halt, then EN idles the boost).
PowerBoost EN: https://learn.adafruit.com/adafruit-powerboost-1000c-load-share-usb-charge-boost

### Recommended pattern (software level-read — works for both power paths)
Wire **COM → a spare GPIO (BCM17)**, throws to GND / 3.3 V, and **read it as
a stable two-state level in software** (gpiozero `Button`/`DigitalInputDevice`):
- **Hold = locked:** software input-lock — the wheel daemon ignores
  scroll/taps (true iPod Hold behaviour), screen can dim.
- **Held locked > N seconds:** trigger `sudo poweroff`; PiSugar's
  `safe_shutdown_level` then cuts power. Power-*on* is PiSugar's own
  button / `auto_power_on` on charge.
This matches how the Hold switch is *meant* to be read (a lock/mode input),
and avoids the momentary-vs-latching mismatch. gpiozero hold recipe:
https://gpiozero.readthedocs.io/en/stable/recipes.html

PiSugar software side for reference: taps run shell scripts
(single `<0.5s` / double / long `>1s`), `safe_shutdown_level`, DS3231 RTC
wake via `auto_wake_time` — all already wired in TapBox's `power.sh`.
https://github.com/PiSugar/pisugar-power-manager-rs/blob/master/doc/config.md

---

## 5. What pipod reuses from TapBox vs. builds new

**Reuse unchanged:** go-librespot fork (Spotify Connect + on-disk cache),
mpv + the `tapbox` package (`library`, `content`, `radio`, `boxapi`,
`spotify`), **`output.py` (already has `bt` + `local` I²S outputs — the jack
IS "local")**, `power.sh` (PiSugar curve/taps/idle/RTC; `hat-audio-on`
already applies the needed overlay), `btwatchd`, the daemon/PWA.

**Build new (the genuinely new pieces — see [SOFTWARE.md](./SOFTWARE.md)):**
1. `clickwheel/click.c` — wheel reader (pigpio, DMA 1 µs), Dupont-derived.
2. `src/podui.py` — iPod-style **scrolling-list UI** on the TFT + input
   router. TapBox's `ui.py` is a 4-button 240×240 model — wrong nav
   paradigm for a wheel — so podui is new, but reuses `boxapi` + `ui.py`'s
   album-art disk cache and marquee logic.
3. `src/holdswitch.py` — Hold-switch lock + safe-shutdown daemon.
4. `install-pipod.sh` — overlay + systemd units, layered on TapBox's
   installer.

---

## Open questions / things to verify with hardware in hand
- **PiSugar 3 stack thickness** vs. your chosen shell's internal depth (§2).
- **Which wheel controller** you have (Synaptics vs Cypress) → bit order (§1).
- **Hold-switch pad map** — meter it (§4).
- **Photo 60 GB internal depth** — community number, not an Apple spec (§2).
- **HP-amp choice** — line-out-only may be "good enough" for your earbuds;
  decide after listening (§3).
