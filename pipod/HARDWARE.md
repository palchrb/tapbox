# pipod — hardware: BOM, pin map, wiring

Design decisions here follow from [RESEARCH.md](./RESEARCH.md). Verify the
⚠️ items against your actual parts before soldering.

## Bill of materials

| # | Part | Notes |
|---|---|---|
| 1 | **4th-gen click-wheel iPod or iPod Photo** (thick capacity if using PiSugar) | 8-pin wheel — the key buy, RESEARCH §0. **Not** an "iPod Classic" 6G/7G (14-pin, no driver). Gut HDD + logic board; keep shell + wheel (+ Hold switch & jack cutout). |
| 2 | Raspberry Pi Zero 2 W | 65×30 mm, ~5.2 mm tall |
| 3a | **Primary power: flat LiPo 2000–2500 mAh + Adafruit PowerBoost 1000C** | Dupont's proven route; bigger battery, thinner, latching Hold→`EN` = clean on/off (RESEARCH §2/§4). Optional MAX17048 for battery %. |
| 3b | *Alt power:* PiSugar 3 (1200 mAh) | "easy mode": pogo-pin stack, I²C gauge, RTC, safe-shutdown, `power.sh` support. ⚠️ ~11–13 mm stack → needs a thick shell. |
| 4 | 0.5 mm **8-pin FPC breakout** | taps the wheel ribbon without soldering to the flex |
| 5 | **GY-PCM5102A** I²S DAC module | line-out; tie **XSMT high** in hardware → frees BCM25 (see below) |
| 6 | Small **headphone amp** (PAM8908 or TPA6132A2 breakout) | after the DAC line-out, for real 16–32 Ω drive. Optional if line-level is "enough". |
| 7 | 3.5 mm stereo jack | reuse the iPod's own jack cutout |
| 8 | **2″ SPI TFT, ST7789, 320×240** | e.g. Waveshare 2″ / Adafruit 2.0″ IPS. Replaces iPod screen. |
| 9 | (optional) 10×2 mm vibration disc + 2N3904 + ~1 kΩ base R | haptic click feedback on BCM26 (Dupont-style) |
| 10 | (reuse) iPod **Hold switch** (SPDT slide) | lock + safe-shutdown input |
| 11 | Enamel/silicone wire, Kapton, a printed internal bracket | isolate boards from the metal case |

**Do not** buy: MAX98357 (mono speaker amp, wrong for a jack); any "iPod
wheel → I²C adapter" (doesn't exist); a 5G Video/Classic or mini wheel
unless you're prepared to reverse-engineer the 14-pin/Molex variant.

## DAC / amp options for the jack (BOM #5–6)

Any of these feeds the 3.5 mm jack. Chosen default is the PCM5102 route
(least Pi-side fuss, matches Vibb's `hifiberry-dac` "local" path). Others
are alternatives — pick per taste; details in RESEARCH §3.

| Option | Chips | Headphone amp? | Pi integration | Notes |
|---|---|---|---|---|
| **PCM5102A + small amp** *(default)* | PCM5102A + PAM8908/TPA6132 | via the added amp | easy — `hifiberry-dac`, no MCLK | 2 boards; XSMT→3V3 frees BCM25 |
| Pirate Audio: Headphone Amp | PCM5100A + PAM8908 | ✅ built-in | easy — `hifiberry-dac` + `gpio=25` | pHAT; `gpio=25` clashes w/ wheel Data → remap wheel |
| **Adafruit TLV320DAC3100** (#6309) | TLV320DAC3100 | ✅ built-in + onboard jack | ⚠️ harder — `tlv320aic31xx` overlay, likely MCLK; adapt `output.py` card check | *alt:* all-in-one DAC+amp+jack, **STEMMA QT/I²C** control; nice if you want Qwiic + one board |
| USB DAC dongle | (varies) | ✅ built-in | trivial — class-compliant | eats the micro-USB OTG port; Dupont's own fallback |
| HiFiBerry DAC2 Pro | PCM5122 + TPA6133 | ✅ built-in | easy — `hifiberry-dacplus` | great sound but full-HAT size (too big inside) |

The **TLV320DAC3100** is the Qwiic/STEMMA-friendly all-in-one (DAC + real
headphone amp + 3.5 mm jack in one small board), but its I²C is for
*configuration* — audio still rides I²S (BCLK/LRCLK/DIN on GPIO), it needs a
`tlv320aic31xx` device-tree overlay (+ probably an MCLK via PWM/GPCLK), and
it won't appear as `sndrpihifiberry`, so pipod's `output.py` card detection
would need a small tweak. Kept as an alternative, not the default.

## GPIO pin budget (BCM)

Chosen so nothing collides. The one real hazard is **BCM25**: the wheel's
Data line (Dupont's `click.c`) *and* the Pirate-Audio-style DAC-enable both
want it. pipod resolves this by using a **bare PCM5102 with XSMT tied high
in hardware**, so we omit `gpio=25=op,dh` and BCM25 stays the wheel's.

| BCM | Physical | Used by | Signal |
|---|---|---|---|
| 2 | 3 | MAX17048 fuel gauge | I²C SDA (addr 0x36) |
| 3 | 5 | MAX17048 fuel gauge | I²C SCL |
| 18 | 12 | I²S DAC | BCLK |
| 19 | 35 | I²S DAC | LRCLK |
| 21 | 40 | I²S DAC | DATA (DOUT→DIN) |
| 23 | 16 | Click wheel | **Clock** |
| 25 | 22 | Click wheel | **Data** (open-drain, needs pull-up) |
| 26 | 37 | Haptic (opt.) | 2N3904 base → vibration motor |
| 10 | 19 | TFT | SPI0 MOSI |
| 11 | 23 | TFT | SPI0 SCLK |
| 8 | 24 | TFT | SPI0 CE0 / CS |
| 24 | 18 | TFT | DC (data/command) |
| 27 | 13 | TFT | RST |
| 12 | 32 | TFT | Backlight (PWM0) |
| 17 | 11 | Hold switch | COM (level read) |
| — | 1,17 | 3.3 V rail | wheel VBat, DAC VIN, TFT VCC |
| — | 6,9,… | GND | common ground |

Free after this: BCM 4,5,6,7,9,13,14,15,16,20,22. Plenty of slack (BT
media buttons are virtual, not GPIO). If you'd rather keep BCM25 for the
DAC-enable style board, remap the wheel Data `#define` in `click.c` to a
free pin instead (e.g. BCM20) — one-line change.

## Wiring detail

### Click wheel (8-pin FFC → breakout → Pi)
Only four wires needed (RESEARCH §1):
```
FFC pin 1 VBat  -> 3.3 V
FFC pin 2 SCK   -> BCM23  (clock)
FFC pin 6 MOSI  -> BCM25  (data; enable internal pull-up in software)
FFC pin 8 GND   -> GND
```
CFG1 (pin 3) / MISO (pin 7): tie per your controller — start with the
Dupont/Gigahawk reference levels and adjust if you read garbage (may
indicate a Synaptics vs Cypress wheel → different bit order).

### I²S DAC (GY-PCM5102A)
```
VIN -> 3.3 V     GND -> GND
BCK -> BCM18     LCK -> BCM19     DIN -> BCM21
SCK -> GND       (forces internal PLL — Pi provides no MCLK)
XSMT-> 3.3 V     (tie HIGH in hardware -> no gpio=25 enable needed)
LOUT/ROUT/AGND -> headphone amp in, or straight to the 3.5 mm jack
```

### TFT (ST7789 320×240, SPI0)
```
VCC->3.3V  GND->GND  SCL->BCM11  SDA->BCM10  CS->BCM8
DC->BCM24  RST->BCM27  BL->BCM12 (PWM backlight)
```

### Hold switch (SPDT, latching)
```
COM   -> BCM17
throwA -> GND        (position = "unlocked")
throwB -> 3.3 V      (position = "locked")   [meter which throw is which]
```
Read as a level in software (`holdswitch.py`); do **not** use
`gpio-shutdown` (edge-triggered, fights a latching switch — RESEARCH §4).

### Power (primary: LiPo + PowerBoost 1000C + MAX17048)
```
LiPo pouch (2000–2500 mAh) -> PowerBoost 1000C BAT / charge via its USB
PowerBoost 5V/GND -> Pi 5V (pin 2/4) and GND
PowerBoost EN -> (optional) Hold switch throw -> GND for a hard on/off
                 [latching switch fits EN; see RESEARCH §4 for the safe-
                  shutdown-first caveat before you rely on a hard cut]
MAX17048 fuel gauge: VIN->3.3V GND->GND SDA->BCM2 SCL->BCM3, sense across
                 the LiPo -> battery % over I2C (addr 0x36)
```
Battery % reaches the UI via a small reader (`src/battery.py`, see
SOFTWARE) — Vibb's `power.sh` battery plumbing is PiSugar-specific and
simply no-ops here. If you instead choose PiSugar 3 (alt), skip PowerBoost
+ MAX17048 and use `power.sh` as Vibb documents it.

## Boot config additions (`/boot/firmware/config.txt`)

```ini
# --- pipod audio: I2S DAC (PCM5102, XSMT tied high => no gpio=25) ---
dtparam=i2s=on
#dtparam=audio=on          # leave OFF; I2S DAC replaces onboard
dtoverlay=hifiberry-dac    # same overlay Vibb output.py expects ("local" pcm)

# --- pipod display: SPI for the ST7789 TFT ---
dtparam=spi=on

# NOTE: do NOT add dtoverlay=gpio-shutdown — the Hold switch is handled in
# software (holdswitch.py). PiSugar owns power via I2C.
```

This is deliberately the **same `hifiberry-dac`** that Vibb's
`vibb-power hat-audio-on` writes — minus the `gpio=25=op,dh` line — so the
existing `output.py` "local" output path works unchanged. See
[SOFTWARE.md](./SOFTWARE.md) for the install flow.

## Fit / assembly notes
- Depth is the only tight axis (RESEARCH §2). Dry-fit the Pi+PiSugar stack
  against the shell's internal depth **before** committing.
- Print a thin internal bracket to keep boards off the metal case (shorts).
- Route the wheel FFC to the breakout first; it's the most fragile part.
- Keep the DAC analog ground away from the Pi's switching noise; short
  jack leads.
