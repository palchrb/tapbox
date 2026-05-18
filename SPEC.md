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

Target prototype cost: ~500 NOK BOM. Target Kickstarter MSRP: ~1500-2000 NOK.

| Component | Choice | Approx cost (NOK) | Rationale |
|---|---|---|---|
| SBC | Raspberry Pi 4 Model B (2GB) | ~600 | Headroom for v2 voice features; Pi Zero 2 W insufficient for local Whisper STT |
| Audio | HiFiBerry MiniAmp (3W stereo, I2S) | ~200 | Standard Pi audio HAT, well-supported |
| Speaker | 3W full-range, 4Ω | ~50 | Driven by MiniAmp |
| Battery | PiSugar 3 (5000mAh) or equivalent | ~250 | 5-10h playback target |
| RFID | PN532 (I2C) | ~50 | Native NDEF support, reads both Mifare Classic and NTAG |
| Microphone (v2-ready) | INMP441 I2S MEMS | ~30 | Reserved for push-to-talk voice in v2; ship with port even if not enabled |
| Push-to-talk button (v2-ready) | Tactile GPIO button | ~10 | Hardware ready, software optional |
| Control buttons | 3× tactile GPIO (next/pause/volume) | ~30 | Independent of RFID |
| microSD slot | Built-in to Pi | 0 | Used for OS + local file storage |
| Enclosure | 3D-printed PLA, child-safe corners | ~50 | Replaceable; offer multiple designs |
| Misc (wiring, screws, USB-C charging port) | | ~80 | |
| **Total** | | **~1350 NOK** | |

Hardware reserves we're building in but not activating in MVP:
- Microphone + PTT button → enables voice control in v2 without hardware revision

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
- [ ] Hosted onboarding web page at tapbox.example.com explains setup and offers web-flasher
- [ ] PWA web flasher (esptool.js equivalent for Pi: pre-built SD card images downloadable)
- [ ] RFID/NFC card reading (Mifare Classic UID + NTAG NDEF)
- [ ] Card → Spotify playlist/track/album/podcast mapping
- [ ] Card → local mp3 file mapping (microSD)
- [ ] Bluetooth A2DP sink mode (toggle in web UI; pair via long-press)
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
- **Spotify offline mode:** Not possible with librespot. Tonies users will miss this. Mitigation: microSD slot for local files, BT A2DP for offline use. Accept the limitation in positioning.
- **Captive portal UX on iOS vs Android:** Subtle differences in how captive portals are triggered and dismissed. Needs real-device testing across iOS 16+/17+/18+ and Android 11+/12+/13+/14+/15+.
- **Battery life realism:** 5-10h target is optimistic for Pi 4. May need to lock CPU governor or move to CM4 for better idle. Measure early.
- **Boot time:** Pi 4 cold boot ~25-30s. Acceptable for "fixed appliance" usage; may need optimization (custom init, removed services) to feel snappy.
- **Audio quality at 3W:** Adequate for kids' content but won't impress audiophiles. Don't oversell.
- **Kid-voice STT (for v2):** Whisper on Pi 4 is borderline. May force a Pi 5 upgrade for v2.

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
