# tapbox — MVP Specification

**Version:** 0.1 (draft)
**Last updated:** 2026-05-18
**Status:** Design phase, pre-implementation

---

## 1. Product Vision

A portable, battery-powered, RFID-controlled speaker for children that plays content from the family's existing music subscriptions — not from a proprietary content store.

The kid taps an RFID card on the speaker; the speaker plays the Spotify playlist, podcast, or local file mapped to that card. Cards are programmable by parents through a polished web interface.

**One-sentence positioning:** *"An open Tonies/Yoto, for families who already pay for Spotify."*

## 2. Target Customer

- Tech-conscious parents (probably devs, designers, makers, or simply Spotify-Family subscribers)
- Already paying for Spotify Premium, Tidal, Apple Music, or similar
- Irritated by the proprietary lock-in of Tonies / Yoto / Toniebox
- Comfortable with "set this up once, then hand it to the kid"
- Values privacy (no always-listening, no ad tracking)
- 1-2 kids, ages 2-10

This is **not** the same audience that buys Tonies in a department store. It is a smaller, more discerning, more loyal segment. We are not chasing mass market.

## 3. Differentiation

| | Tonies | Yoto | Jooki | Phoniebox (DIY) | tapbox |
|---|---|---|---|---|---|
| Open content (BYO Spotify) | ❌ | Partial | ✅ | ✅ | ✅ |
| Polished onboarding | ✅ | ✅ | ✅ | ❌ | **✅ key bet** |
| RFID/NFC cards | ✅ (figurines) | ✅ | ✅ | ✅ | ✅ |
| Offline-capable (away from home) | ✅ | ✅ | Partial | Local files | Partial (local files + BT) |
| Hackable / extensible | ❌ | ❌ | ❌ | ✅ | ✅ |
| Privacy (no always-listening) | ✅ | ✅ | ✅ | ✅ | ✅ (PTT for v2 voice) |
| Multi-service streaming | ❌ | ❌ | Spotify only | Spotify only | Spotify + Tidal + BT + local |

**Where we win:** polished onboarding + multi-service + open/hackable. We do not try to match Tonies on raw offline portability.

## 4. Non-Goals

Explicitly **not** trying to be:
- A mass-market Tonies replacement (we're indie hardware scale)
- A Toniebox-style "works anywhere offline" device (Spotify streaming requires internet)
- A general-purpose smart speaker (no Alexa, no voice control in MVP)
- A high-end audio device (kid content doesn't need audiophile DAC)
- A platform with proprietary content store (revenue model is hardware sales only)

## 5. Hardware BOM

Target prototype cost: ~870 NOK BOM. Target Kickstarter MSRP: ~1100-1500 NOK.

| Component | Choice | Approx cost (NOK) | Rationale |
|---|---|---|---|
| SBC | Raspberry Pi Zero 2 W | ~150 | Runs librespot + Music Assistant + RFID daemon comfortably; small form factor; low idle power for long battery life |
| Audio amp + DAC | [Pimoroni Audio Amp SHIM](https://shop.pimoroni.com/products/audio-amp-shim-3w-mono-amp) (MAX98357A I2S DAC + Class-D amp, 3W mono, 5V) | ~110 | Slim SHIM form factor (fits between Pi and other HATs); well-supported by Pimoroni; sourced from a reliable EU/UK retailer rather than Aliexpress. 3W is well-matched to the 4W driver under realistic volume-capped usage; amp headroom matters less than driver size for perceived quality. |
| Main driver | Dayton Audio CE Series 70mm 4Ω full-range (CE70P-4 or similar) | ~120 | Real fullrange driver, full midrange + decent treble. Mono in this form factor is better than weak stereo. |
| Passive radiator | Tang Band 2" passive radiator (or Dayton equivalent) | ~60 | Extends low-end response without amp power cost; critical for "not tinny" feel |
| Battery + management | PiSugar 3 1200mAh (with optional larger LiPo swap, pending JST verification) OR Adafruit PowerBoost 1000C + 5000mAh LiPo | ~250-500 | 8-10h playback target on 5000mAh. See section 10 for open questions. |
| RFID | PN532 (I2C) | ~50 | Native NDEF support, reads both Mifare Classic and NTAG |
| Control buttons | 3× tactile GPIO (next/pause, vol up, vol down) | ~30 | Independent of RFID; optional power button on PiSugar |
| microSD card | 16GB Class 10 | ~60 | OS + local files + cached metadata |
| Enclosure | 3D-printed PLA/PETG, child-safe corners; designed around driver + passive radiator volume | ~80 | Larger than initial spec (~10×10×8 cm minimum) to give the 70mm driver room to breathe |
| Misc (wiring, USB-C charging port, 5× NTAG215 starter cards) | | ~80 | |
| **Total** | | **~990-1240 NOK** | |

**Voice (v2) is explicitly excluded from MVP hardware.** Pi Zero 2 W is borderline for local Whisper STT and would need cloud STT or a hardware upgrade to support voice. Pre-paying ~450 NOK per unit for Pi 4 "headroom" for a feature we've deferred is the wrong call. Lean approach: ship MVP without voice hardware, validate with real users, then deliberately design v2 hardware if voice ships.

## 6. Software Stack

| Layer | Choice | License | Notes |
|---|---|---|---|
| OS | Raspberry Pi OS Lite (Bookworm or current Legacy Lite) | Debian mix | Standard Pi OS |
| Music orchestration | [Music Assistant](https://music-assistant.io/) | Apache 2.0 | Handles Spotify, Tidal, podcasts, local files in one abstraction |
| Spotify backend | librespot (via Music Assistant) | MIT | Reverse-engineered Spotify Connect — see risks |
| Audio playback | mpv (driven by Music Assistant) | GPL-2.0 | Used as binary, no linking issues |
| RFID daemon | Custom Python (~200 LOC) | Apache 2.0 | Reads PN532, looks up mapping, calls MA API |
| Web UI | Custom (React or Svelte + Vite + Tailwind, TBD) | Apache 2.0 | Onboarding + admin |
| Web server | Caddy (auto-HTTPS via local CA) or nginx + selfsigned | Apache 2.0 / BSD | TLS solves mixed-content for any future hosted PWA |
| Captive portal | comitup or NetworkManager + custom | various | First-boot Wi-Fi setup |
| Service supervision | systemd | LGPL | Native |
| Bluetooth A2DP | bluez-alsa | LGPL | Pi as BT sink — escape hatch for any music app |
| mDNS | avahi | LGPL | `tapbox.local` discoverability |

**Cherry-picked from Phoniebox v3 (MIT) as reference, not forked:**
- RFID reader hardware abstraction pattern (`src/jukebox/components/rfid/hardware/`)
- Autohotspot scripts approach
- GPIO button recipes via gpiozero

## 7. MVP Feature List (v1.0)

### Must have
- [ ] First-boot Wi-Fi provisioning via captive portal (AP mode)
- [ ] Pre-flashed microSD ships with device — no user flashing required (massive UX win)
- [ ] Hosted onboarding web page at tapbox.example.com walks user through first power-on
- [ ] RFID/NFC card reading (Mifare Classic UID + NTAG NDEF)
- [ ] Card → Spotify playlist/track/album/podcast mapping
- [ ] Card → local mp3 file mapping (microSD)
- [ ] **Parent app: drag-and-drop upload of local audio files** (mp3, m4a, wav, opus) via web UI — files stored on microSD, mapped to next-tapped card. Critical for offline use case (cabin/car/grandma without WiFi) — see section 10.
- [ ] **Bluetooth A2DP source mode (output to BT headphones / external speakers):** Pair via web UI, route audio output to BT device. Critical for quiet listening (bedtime, shared spaces) and the v2 "tapbox Lite" architecture preview.
- [ ] Bluetooth A2DP sink mode (toggle in web UI; pair via long-press) — phone streams TO tapbox (escape hatch for unsupported services like Apple Music)
- [ ] Polished admin web UI: list cards, add/edit/delete mappings, see currently-playing
- [ ] Card programming: paste Spotify URL → write NDEF to next-tapped NTAG card OR save UID mapping
- [ ] Physical controls: next, pause/play, volume up/down
- [ ] Battery indicator (LED or in web UI)
- [ ] Graceful shutdown via long-press
- [ ] Auto-restart on crash (systemd)
- [ ] OTA updates (`git pull` + service restart, gated by signed releases)
- [ ] Factory reset (long-press combo, wipes Wi-Fi + mappings)

### Should have
- [ ] Tidal support via Music Assistant
- [ ] Internet radio (NRK Radio, others)
- [ ] Sleep timer
- [ ] Volume cap for safety
- [ ] Export/import mappings (JSON download/upload)
- [ ] Multi-language UI (NO, EN at minimum)

### Won't have in MVP
- Voice control (push-to-talk) — hardware ready, software is v2
- Cloud-relay for remote admin — v2 if there's demand
- Multi-device sync — v2
- Apple Music — not feasible in indie scale
- YouTube Music — ToS risk too high

## 8. Post-MVP Backlog

**v1.1-1.2 (polish):**
- More language support
- Custom card pack designs (printable templates)
- Battery health monitoring
- Audio EQ presets for kids' content
- Bedtime mode (fades out, won't restart)

**v2 (significant):**
- Push-to-talk voice control with local Whisper STT + Claude/GPT intent
- Cloud-relay for remote management
- Multi-device household sync
- Spotify Connect Partner certification (if scale justifies cost)
- Card pack marketplace (parents share curated collections)

## 9. Onboarding Flow (Unboxing → First Card Scan)

The polish target. We want this to take **<10 minutes** from unboxing to "kid taps card, music plays."

```
1. User unboxes: device, USB-C charger, 5 blank NTAG215 cards, quickstart card with QR code

2. QR code → tapbox.example.com/setup → polished landing page
   "Welcome. Plug in your device and wait 30 seconds for the green LED."

3. User powers on device. Pi boots (~25s). Green LED solid when ready.

4. Web page: "Connect to Wi-Fi 'tapbox-XXXX' on your phone."
   Page auto-detects when device's hotspot appears on the network and advances.

5. User connects phone to tapbox-XXXX. iOS/Android shows captive portal automatically.

6. Captive portal (styled identical to hosted page):
   - Select home Wi-Fi network from scan results
   - Enter password
   - "Connecting..." → device drops AP, joins home network

7. Phone reconnects to home Wi-Fi. Page advances: "Setting up music services."

8. OAuth flow: "Connect Spotify" → standard Spotify OAuth (PKCE, no client secret on device)
   - Optional: "Connect Tidal" / "Skip"

9. "Speaker test" — plays a 3-second chime. Volume calibration slider.

10. "Program your first card" — paste any Spotify URL, tap a blank card to device.
    Device writes NDEF, confirms.

11. "Done. Hand it to your kid." — link to admin panel for adding more cards.
```

Every step has: clear status, retry path on failure, what-to-do-if-stuck. No JSON. No SSH. No SD card editing.

## 10. Risks & Open Questions

### Legal / business
- **Spotify ToS via librespot:** Risk that Spotify sends C&D at any time. Historically not enforced at indie scale. Mitigation: don't use Spotify trademarks, don't claim "Spotify Connect Certified". Watch for enforcement signals.
- **Tidal API ToS:** Same risk profile.
- **EU Toy Safety Directive (EN 71):** Required if marketing to under-14s. ~30-80k NOK testing cost. Mitigation: market initially as "for families," not "for children" specifically.
- **UN38.3 battery certification** for shipping: ~50k NOK. Required to ship Li-Po internationally.

### Technical
- **No offline mode for streaming services:** librespot, Tidal, and other DRM-protected services cannot cache content. Tonies users will miss the "works anywhere offline" magic. Mitigations: (1) microSD slot for local audio, (2) drag-and-drop upload via parent web app so non-technical parents can move kid's favorite content offline, (3) auto-cache podcast episodes when on WiFi, (4) explicit positioning: "Home-first; bring your own MP3s for offline use." Don't claim feature parity with Tonies on offline.
- **Captive portal UX on iOS vs Android:** Subtle differences in how captive portals are triggered and dismissed. Needs real-device testing across iOS 16+/17+/18+ and Android 11+/12+/13+/14+/15+.
- **Battery life realism:** 8-12h target on Pi Zero 2 W with 5000mAh is achievable but assumes idle CPU governor and audio off when not playing. Validate with real measurements before committing in marketing.
- **Boot time:** Pi Zero 2 W cold boot ~25-35s on default Pi OS. May need optimization (custom init, removed services, faster SD card) to feel snappy on power-on. Likely fine for "always-on, deep-sleep" usage pattern.
- **Audio quality target:** Aiming for "clearly better than Tonies/Yoto" via 70mm Dayton driver + Tang Band passive radiator + MAX98357A 3W Class-D amp (via Pimoroni Audio Amp SHIM). Driver size and passive radiator do most of the heavy lifting — ~85% of perceived quality difference vs Tonies comes from the 70mm driver and the radiator, not amp headroom. 3W is well-matched to the 4W driver under volume-capped usage (which is mandatory for hearing safety anyway). NOT trying to match Sonos One (different product class; plug-in stereo with separate woofer + tweeter). Validate by side-by-side listening test against Tonies before locking enclosure design — if test fails, the next upgrade lever is enclosure tuning (vent/port design) or stepping up to TAS5805M with bridged mono (~10W headroom).
- **Kid-voice STT (for v2):** Local Whisper not viable on Pi Zero 2 W. v2 voice will likely require either cloud STT (no hardware change) or a hardware revision to Pi 4/CM4. Decide later based on real user feedback.

### Open product questions
- **Card programming UX:** Write NDEF to NTAG (preferred) vs. UID mapping (works with Mifare Classic too). Support both? Default to NDEF?
- **Should we sell pre-printed cards?** Themed packs (animals, fairy tales, etc.) increase perceived value but add inventory/SKU complexity.
- **Subscription business model?** Cloud-relay + card-pack-marketplace could justify ~30-50 NOK/month. Probably not in MVP, but worth keeping the door open architecturally.
- **3D-printed enclosure vs. injection-molded:** 3D printing is fine up to 100 units; beyond that, injection molding pays back. Affects launch unit count.

## 11. Reference Prior Art

- **Phoniebox / RPi-Jukebox-RFID v3** (MIT) — solid reference for RFID+GPIO+audio on Pi. Cherry-pick patterns, do not fork.
- **Jooki** — commercial precedent; instructive case study (bankrupt 2020, relaunched). Read their post-mortem if available.
- **Music Assistant** — our music orchestration layer; aligned community.
- **Home Assistant Voice / Wyoming protocol** — relevant for v2 voice features.

---

*This is a living document. Update as decisions are made and assumptions are validated or invalidated.*
