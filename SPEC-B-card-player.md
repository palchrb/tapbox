# tapbox Concept B — Card Player (the Yoto competitor)

**Version:** 0.1 (carried over from tapbox SPEC v0.10)
**Last updated:** 2026-07-05
**Status:** Design phase, pre-implementation
**Parent doc:** [SPEC.md](SPEC.md) (shared platform)

---

## 1. Idea

A portable, battery-powered, card-controlled speaker. The kid inserts a card
into a slot; the box plays the Spotify playlist, podcast, or local content
mapped to that card. Card out = pause. Cards are programmable by parents
through the PWA.

## 2. Hardware BOM

Target prototype cost: ~1180 NOK BOM. Target Kickstarter MSRP: ~1400-1700 NOK.

| Component | Choice | Approx cost (NOK) | Rationale |
|---|---|---|---|
| SBC | Raspberry Pi Zero 2 W | ~150 | Runs go-librespot + mpv + daemons comfortably (512MB rules out Music Assistant); low idle power |
| Audio amp + DAC | [Pimoroni Audio Amp SHIM](https://shop.pimoroni.com/products/audio-amp-shim-3w-mono-amp) (MAX98357A I2S, 3W mono, 5V) | ~110 | Slim SHIM form factor; 3W well-matched to the 4W driver under volume-capped usage |
| Main driver | Dayton Audio CE Series 70mm 4Ω full-range (CE70P-4 or similar) | ~120 | Real fullrange driver; mono in this form factor beats weak stereo |
| Passive radiator | Tang Band 2" passive radiator (or Dayton equivalent) | ~60 | Extends low end without amp power cost; critical for "not tinny" |
| Battery charger + boost | Adafruit PowerBoost 1000C (USB-C in, 5V boost, LBO pin for graceful shutdown) | ~200 | Plug-and-play; within Pi + SHIM peak draw (~1.3A) |
| Battery | Adafruit 6600mAh LiPo w/ protection PCB + JST-PH | ~300 | ~8h streaming baseline; ~14h optimized; ~28h cached-local |
| RFID | PN532 (I2C) | ~50 | NDEF support, reads Mifare Classic and NTAG |
| Card slot sensor | SMD detector switch at the slot (Panasonic ESE-11 / Omron D3SH class, 3-5mm, gold contacts; D2F-01FL for hand-built prototypes) | ~10 | See §5 — the wake strategy. Riskorn-sized on the product PCB; size is only a prototype concern |
| Control buttons | Tactile GPIO: play/pause, next, prev, vol up, vol down + optional power button wired to PowerBoost EN | ~40 | All routed via gpio-keys overlay → existing buttons.py (incl. volume, implemented) |
| microSD card | 16GB Class 10 | ~60 | OS + local files + podcast cache |
| Enclosure | 3D-printed PLA/PETG, child-safe corners; sized for driver + passive radiator (~10×10×8 cm min) + shallow card slot | ~80 | Card sticks up visibly from the slot (Yoto pattern) |
| Misc (wiring, USB-C port, 5× NTAG215 starter cards) | | ~80 | |
| **Total** | | **~1180 NOK** | |

**Voice (v2) is explicitly excluded from MVP hardware.** Lean approach: ship
MVP without voice hardware, validate with real users, then deliberately design
v2 hardware if voice ships.

## 3. MVP Feature List (v1.0)

### Must have
- [ ] First-boot Wi-Fi provisioning via captive portal (AP mode)
- [ ] Pre-flashed microSD ships with device — no user flashing required
- [ ] Hosted onboarding web page walks user through first power-on
- [ ] RFID/NFC card reading (Mifare Classic UID + NTAG NDEF)
- [x] Card → Spotify/NRK/RSS/local mapping (platform: cards.json + player.py)
- [x] Card slot semantics: card in = play (with resume), card out = pause (platform: rfid.py slot mode, 2026-07-05)
- [ ] **Parent app: drag-and-drop upload of local audio files** (mp3, m4a, wav, opus) — offline use case (cabin/car/grandma)
- [x] **Bluetooth A2DP source mode:** button-first pairing (auto-pair strongest nearby audio device), bond persists, auto-reconnect (platform, hardware-validated)
- [ ] Bluetooth A2DP sink mode (toggle in PWA) — phone streams TO tapbox (escape hatch for Apple Music etc.)
- [ ] Polished admin PWA: list cards, add/edit/delete mappings, now-playing
- [ ] Card programming: paste URL → write NDEF to next-tapped NTAG OR save UID mapping
- [x] Physical controls: play/pause, next, prev, volume up/down (platform: buttons.py + tapboxd /volume)
- [ ] Battery indicator (LED or PWA; PiSugar-based rig tooling exists)
- [ ] Graceful shutdown via long-press
- [x] Auto-restart on crash (systemd)
- [x] OTA updates (`git pull` + idempotent install.sh; gate by signed releases later)
- [ ] Factory reset (long-press combo, wipes Wi-Fi + mappings)

### Should have
- [x] Internet radio (NRK live channels via mpv/HLS)
- [x] Podcast auto-cache (newest-N download while playing, NRK; generic RSS pending)
- [ ] Sleep timer
- [ ] Volume cap for safety
- [ ] Export/import mappings (JSON download/upload)
- [ ] Multi-language UI (NO, EN at minimum)

### Won't have in MVP
- Voice control — v2
- Cloud-relay for remote admin — v2 if demand
- Multi-device sync — v2
- Tidal (BT sink covers it indirectly) / Apple Music / YouTube Music

## 4. Onboarding Flow (Unboxing → First Card)

The polish target: **<10 minutes** from unboxing to "kid inserts card, music plays."

```
1. Unbox: device, USB-C charger, 5 blank NTAG215 cards, quickstart card with QR
2. QR → hosted setup page: "Plug in your device, wait for the green LED."
3. Power on (~25s boot). Green LED solid when ready.
4. "Connect to Wi-Fi 'tapbox-XXXX' on your phone." Page auto-advances.
5. Captive portal: pick home Wi-Fi, enter password, device joins.
6. "Connect Spotify" — zeroconf (LOCKED, hardware-validated): open Spotify,
   tap the devices icon, pick 'tapbox'. Credentials persist on the device.
   No password entry, no OAuth, no developer-app registration.
7. Speaker test chime + volume calibration slider.
8. "Program your first card" — paste any link, insert a blank card. Done.
9. "Hand it to your kid." → link to admin panel.
```

Every step has: clear status, retry path, what-to-do-if-stuck. No JSON, no SSH.

## 5. Card & RFID design (DECIDED 2026-07-05)

**Card slot + detector switch (Yoto model).** The NFC field is the power cost
(~30-50mA while on); the switch, not the radio, is the presence sensor:

- Idle: PN532 fully unpowered (power-gated or held in reset). Only listener is
  the detector switch on a GPIO with internal pull-up — 0 mA standby.
- Card inserted: switch edge → power the reader → **one single read** (slot
  guarantees mm-precise antenna alignment) → reader off. ~100ms of RF per card
  change instead of all-day polling.
- Card removed: opposite edge → pause via tapboxd (bookmark saved; same card
  resumes instantly — tapboxd keeps the session loaded). NFC is never involved
  in stop detection (that's what forces Toniebox-style polling).
- UX: physical state == audio state. Constrains the product to **cards only**
  (no figurines). Shallow slot, card sticks up visibly (kid can see + pull it;
  discourages posting other objects).
- Switch: SMD detector switch on the product PCB (Panasonic ESE-11 ~3.4×2.7mm
  or Omron D3SH ~4.9×3.9mm, gold contacts, 0.1-0.5N, 100k+ cycles — the SD-card
  -slot component class). Hand-built prototypes: Omron D2F-01FL. IR break-beam
  rejected (LED draws mA continuously).
- Fallback for slot-less designs: PN532 IRQ-driven InAutoPoll. Same daemon
  architecture; enclosure design picks the wake source.
- **Status: implemented and tested in `rfid.py` (slot mode)** — simulate with
  `SLOT_GPIO=file:/tmp/card` + `FAKE_UID` before the switch/reader arrive.
- Backlog (from Phoniebox): command cards — a card mapping to an action
  (`cmd:stop` bedtime card) instead of content.

## 6. Technical risks & open questions

- **Audio quality target:** "clearly better than Tonies/Yoto" via 70mm driver +
  passive radiator; driver size does ~85% of the perceived difference, not amp
  headroom. Validate by side-by-side listening test before locking the
  enclosure; upgrade levers: enclosure tuning, then TAS5805M bridged mono (~10W).
- **Battery life realism:** 8h/14h/28h numbers must be re-validated on final
  hardware with the CSV logger before any marketing use. Rig measurement
  (2026-07-05, in progress): ~0.7W total system draw during BT playback —
  but note the rig outsources amplification to the BT speaker's own
  battery. Concept B drives its own MAX98357A + 70mm driver: add
  ~0.3-0.6W average at volume-capped kid levels (3W is peak, music crest
  factor keeps the average low) ≈ **~1.0-1.3W playing on the built-in
  speaker**. Battery sizing at ~1.1W: 3300mAh ≈ 10-11h (all-day, ~2h
  charge at 2A), 6600mAh ≈ 20-22h (Yoto-tier marketing numbers, slower
  charge/heavier). Positioning choice, not a technical one — decide after
  the rig's full discharge run pins the base draw.
- **Charge time vs battery size (found on the rig 2026-07-05):** the rig's
  PiSugar 3 charges at up to 2A (input spec 5V-3A max; 3A peak with chip at
  ~80°C per PiSugar docs) — measured: 26%→full in ~70 min on a 5V/2A
  charger, i.e. roughly 1.5C on its 1200mAh cell. PiSugar 3 also pauses
  charging every 3s to measure battery voltage, so logged voltage/percent
  during charge is mostly trustworthy. The product concern is the BOM's
  PowerBoost 1000C, which charges at only ~1A — into 6600mAh that is
  **~7-8h charge time**, weak next to Yoto. Options: (a) accept and position
  as "charges overnight" (plausible for a bedtime device), (b) a 2-3A
  charger IC (6600mAh at 2A ≈ 0.3C, gentle → ~3.5h), (c) smaller battery —
  3300mAh + 2A ≈ 2h charge and still solid playtime if the 28h cached-mode
  estimate proves to be overkill, (d) **PiSugar 3 Plus instead of
  PowerBoost+LiPo**: 5000mAh, 3A in / 2.5-3A out (charges in ~2.5h), UPS +
  RTC + power button integrated, and it's the platform already proven on
  the rig — tradeoffs are cost and its full-size-Pi form factor in a
  Zero-based enclosure. Decide after the rig's measured discharge run.
- **Captive portal UX on iOS vs Android:** needs real-device testing.
- **Card programming UX:** NDEF write (preferred) vs UID mapping (works with
  Mifare Classic). Support both? Default NDEF?
- **Pre-printed card packs?** Perceived value vs SKU complexity.
- **Subscription model?** Cloud-relay + card marketplace could justify 30-50
  NOK/mo. Not MVP; keep the door open architecturally.
- **3D-print vs injection molding:** 3D fine to ~100 units; affects launch count.

## 7. Post-MVP Backlog

**v1.1-1.2:** more languages, printable card templates, battery health
monitoring, EQ presets, bedtime mode (fade out, won't restart).

**v2:** push-to-talk voice (cloud STT or Pi 4/CM4 revision), cloud-relay,
household sync, Spotify Connect partner certification (if scale justifies),
**official partner integrations for DRM services** (Storytel — Nordic HQ,
Sonos precedent proves the certified path; pitch once there's volume),
card pack marketplace.
