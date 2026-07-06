# tapbox — Product Specification (platform + two concepts)

**Version:** 0.11 (draft)
**Last updated:** 2026-07-05
**Status:** Design phase; software platform hardware-validated on the test rig

---

The project is split into **one shared software platform** and **two device concepts** built on it:

| | Concept | Spec | One-liner |
|---|---|---|---|
| **A** | Screen navigator | [SPEC-A-explorer.md](SPEC-A-explorer.md) | Today's rig + Pimoroni Pirate Audio (240×240 screen, 4 buttons, mini speaker). Kid browses a parent-curated library on screen. No new mechanics — buildable now. |
| **B** | Card player | [SPEC-B-card-player.md](SPEC-B-card-player.md) | The full Yoto competitor: card slot + detector switch, 70mm driver + 3W amp, physical play/next/prev/volume buttons, custom enclosure. |

Concept A doubles as the validation vehicle for Concept B: same daemon, same PWA library model, same content backends — only the input (buttons+screen vs. cards) and output (HAT speaker vs. built-in driver) differ. Everything learned in A carries over.

## 1. Product Vision

A portable, battery-powered music player for children that plays content from the family's existing music subscriptions — not from a proprietary content store.

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
| RFID/NFC cards | ✅ (figurines) | ✅ | ✅ | ✅ | ✅ (Concept B) |
| Offline-capable (away from home) | ✅ | ✅ | Partial | Local files | Partial (local files + podcast cache + BT) |
| Hackable / extensible | ❌ | ❌ | ❌ | ✅ | ✅ |
| Privacy (no always-listening) | ✅ | ✅ | ✅ | ✅ | ✅ (PTT for v2 voice) |
| Multi-service streaming | ❌ | ❌ | Spotify only | Spotify only | Spotify + NRK + any RSS + BT-sink escape hatch + local |

**Where we win:** polished onboarding + multi-service + open/hackable. We do not try to match Tonies on raw offline portability.

## 4. Non-Goals

Explicitly **not** trying to be:
- A mass-market Tonies replacement (we're indie hardware scale)
- A Toniebox-style "works anywhere offline" device (Spotify streaming requires internet)
- A general-purpose smart speaker (no Alexa, no voice control in MVP)
- A high-end audio device (kid content doesn't need audiophile DAC)
- A platform with proprietary content store (revenue model is hardware sales only)

## 5. Shared Software Platform (hardware-validated in `pi/`)

| Layer | Choice | License | Notes |
|---|---|---|---|
| OS | Raspberry Pi OS Lite (Bookworm) | Debian mix | Standard Pi OS |
| Spotify backend | [go-librespot](https://github.com/devgianlu/go-librespot) (daemon, HTTP API :3678) | GPL-3.0 | Zeroconf login ("pick tapbox in the Spotify app") is the LOCKED onboarding UX. Phone doubles as remote (same Connect session). Separate binary over HTTP, no linking issues |
| Local/podcast/radio playback | mpv (IPC socket) | GPL-2.0 | Used as binary. Resamples everything to 44.1kHz (BT/SBC requirement) |
| Content expansion | `nrk.py` | Apache 2.0 | NRK podcasts/series/live channels via psapi (incremental catalog cache), any RSS feed, local folders. Offline cache: newest-N episode download (mp3 direct, HLS→m4a) |
| Orchestration | `daemon.py` (tapboxd, HTTP API :3679) | Apache 2.0 | THE authority on playback state. `/play /pause /playpause /next /prev /stop /volume /status`. Routes commands to the active source, resumes last target on dead sessions, yields mpv when the phone takes over Spotify, one box-volume knob (mpv softvol / Spotify volume). This API is the backend for the parent PWA and for Concept A's screen UI |
| Playback runner | `player.py` | Apache 2.0 | Per-playback process: Spotify→go-librespot, rest→mpv. Per-card resume bookmarks keyed on episode id; live streams excluded |
| Input daemons | `rfid.py`, `buttons.py` | Apache 2.0 | RFID: poll mode + slot mode (card in = play, card out = pause). Buttons: generic evdev media keys (AVRCP, GPIO via gpio-keys overlay, USB) incl. volume |
| Power tooling | `power.sh`, `idle.py`, PiSugar tap shells | Apache 2.0 | Governor/LED/HDMI/wifi tuning, battery CSV logger, idle auto-poweroff, PiSugar button gestures |
| Web UI (planned) | Custom PWA (framework TBD) | Apache 2.0 | Onboarding + library/card admin against tapboxd |
| Bluetooth A2DP | bluez-alsa | LGPL | Source mode (BT speakers/headsets, auto-reconnect, button-first pairing) and sink escape hatch |
| Service supervision | systemd | LGPL | `install.sh` is idempotent: `git pull && sudo ./pi/install.sh` = full update |

**Verified end-to-end on the Zero 2 W rig (2026-07-04/05):** Spotify (zeroconf + share links + phone remote), NRK podcasts/series/live radio, arbitrary RSS feeds, local folders, offline episode cache, per-card resume, BT auto-reconnect after power cycle, AVRCP + PiSugar buttons, idle shutdown, cold boot.

## 6. Risks (shared)

### Legal / business
- **Spotify ToS via librespot:** Risk that Spotify sends C&D at any time. Historically not enforced at indie scale. Mitigation: don't use Spotify trademarks, don't claim "Spotify Connect Certified". Watch for enforcement signals.
- **EU Toy Safety Directive (EN 71):** Required if marketing to under-14s. ~30-80k NOK testing cost. Mitigation: market initially as "for families," not "for children" specifically.
- **UN38.3 battery certification** for shipping: ~50k NOK. Required to ship Li-Po internationally.
- **DRM services:** BYO-content only in v1 (local files, open RSS, DRM-free audiobooks). Official partner APIs (Storytel's Sonos integration proves the path) are the v2 strategy — no reverse-engineered clients beyond librespot.

### Technical
- **No offline mode for streaming services:** DRM playback always needs a live session + per-track key, so true offline Spotify is off the table regardless of client. Mitigations: local files, drag-and-drop upload in the PWA, podcast auto-cache (implemented), honest positioning: "Home-first; travel content = local files and cached podcasts."
- **No Spotify *bandwidth* cache either (2026-07-05):** Rust librespot caches downloaded (still-encrypted) audio to disk, making repeat plays free — a big deal on mobile hotspots given kids' repeat-heavy listening (~72 MB/h at 160 kbps otherwise). go-librespot, which we use for its control API, has no audio cache. **RESOLVED via fork (2026-07-06):** TapBox now runs [palchrb/go-librespot](https://github.com/palchrb/go-librespot) — upstream + an on-disk cache for the encrypted audio files (LRU, size-limited, keyed by file id, atomic writes; keys still fetched live, mirroring Rust librespot's design — no extra DRM exposure). Pinned in install.sh; config: `cache: {enabled, dir, size_limit}`. Still worth proposing upstream to shed the fork-maintenance cost (rebasing on upstream releases). Note: a cache saves data, not connectivity; fully-offline is covered by the mitigation above only.
- **Backlog idea — WiFi auto-off away from known networks (designed 2026-07-06, not built):** a disconnected wpa_supplicant scan-loops constantly (~10-20mA, 5-10% of playback draw); a small `tapbox-wifi` daemon would fix it. Design: always unblock wifi at boot (systemd-rfkill persists blocks across reboots — same gotcha as BT); while associated, do nothing; after 15 min without a known network, `rfkill block wifi`; then probe every 10 min with a tight ~20s window (one scan sweep, check results against known SSIDs, associate only on match — costs ~0.2-0.3mAh per probe ≈ ~1.5mA average, so ~99% of the saving is kept and a parent's hotspot is found within max 10 min). Manual override + opt-in via tapbox-power (`wifi-on/off`, `wifi-auto-on/off`), intervals in an env file. Never triggers during active streaming by construction (trigger condition is "not associated"). Payoff: whole-trip flights/cabin playback of cached/local content without the scan tax; measure the real saving with the battery logger before shipping claims.
- **Battery life:** validate all marketing numbers with the CSV logger on real hardware (discharge run in progress on the rig).

## 7. Reference Prior Art

- **Phoniebox / RPi-Jukebox-RFID v3** (MIT) — reference for RFID+GPIO+audio patterns (reader ABC, place-not-swipe, same_id_delay). Cherry-pick, do not fork.
- **drewbatchelor.com Pi Zero 2 music player** — same ST7789 240×240 screen + button navigation as Concept A; proof the display UX works on a Zero 2 W.
- **Jooki** — commercial precedent; instructive case study (bankrupt 2020, relaunched).
- **Music Assistant** — dropped: needs 2GB+ RAM vs the Zero 2 W's 512MB.
- **Home Assistant Voice / Wyoming protocol** — relevant for v2 voice features.

---

*This is a living document. Concept-specific detail lives in SPEC-A-explorer.md and SPEC-B-card-player.md.*
