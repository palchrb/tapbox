# tapbox Concept A — Screen Navigator ("Explorer")

**Version:** 0.1 (draft)
**Last updated:** 2026-07-05
**Status:** Concept locked, hardware ordered/owned except the HAT
**Parent doc:** [SPEC.md](SPEC.md) (shared platform)

---

## 1. Idea

Build on the hardware we already have — Pi Zero 2 W + PiSugar 3 — and add a
[Pimoroni Pirate Audio Speaker](https://shop.pimoroni.com/products/pirate-audio-speaker)
(~250 NOK): 240×240 IPS display (ST7789), **4 tactile buttons**, MAX98357A I2S
DAC/amp with a built-in ~1W mini speaker.

The kid navigates a **parent-curated library** on the screen with the four
buttons: pick a service, pick an entry by name, play everything or a single
episode/track. No RFID, no enclosure work, no new mechanics — this is buildable
immediately and doubles as the validation vehicle for the PWA library model
that Concept B (card player) also needs.

Proof the display UX works on a Zero 2 W: drewbatchelor.com's Pi Zero 2 music
player uses the same screen.

## 2. Library model — IMPLEMENTED (2026-07-06)

**Architecture decision: local-first.** The library lives in
`/etc/tapbox/library.json` on the box — menus must render (and cached
content must play) with zero internet (the flight/cabin scenario). A future
parent cloud service is a *sync mirror* of this file for remote admin (v2
cloud-relay), never the source of truth.

Sections are parent-named (free-form, ordered — favourites first), entries
are named links with stable ids. A target is anything `player.py` routes:
Spotify playlist/album/show, NRK podcast/series/live channel, any RSS feed,
local folder.

```json
{ "version": 1, "sections": [
  { "id": "musikk", "name": "Musikk", "entries": [
      { "id": "f50bb730", "name": "Barnesanger",
        "target": "https://open.spotify.com/playlist/...", "order": "auto" } ] },
  { "id": "fortellinger", "name": "Fortellinger", "entries": [
      { "id": "2ccfb219", "name": "Fantorangen",
        "target": "https://radio.nrk.no/podkast/fantorangenfortellinger",
        "order": "auto" } ] } ] }
```

`order` (`auto | newest_first | oldest_first`) sets menu/playback direction
per entry: `auto` = the service's natural order (NRK podcasts newest first,
series/folders oldest first, RSS as the feed lists), explicit values
override when a service guesses wrong (e.g. an RSS audiobook listed
newest-first).

**tapboxd API (implemented + tested):** `GET /library`, `PUT /library`
(validated, atomic), `GET /expand?id=<entry>|target=<url>` → titled episode
list with per-episode `cached` flags (offline-aware menus; Spotify entries
return `kind: "spotify"` = leaf), and `POST /play {"id"|"target",
"episode", "fresh"}` — episode picks rotate the queue there (bookmarked
episode continues at its position, others start from the top; the bookmark
follows). Pre-PWA management CLI: `tapbox-lib add/list/rm/order`.

Screen navigation:

```
Tjenester            Entries (one service)     Entry
┌────────────┐       ┌──────────────────┐      ┌──────────────────┐
│ Spotify    │  A →  │ Barnesanger      │  A → │ ▶ Spill alle     │
│ NRK        │       │ Fantorangen...   │      │ Episode 1        │
│ Podkaster  │       │ ...              │      │ Episode 2 ...    │
│ Lokalt     │       └──────────────────┘      └──────────────────┘
└────────────┘
```

Episode lists come from the existing `nrk.py` expansion (titles included);
Spotify entries play as a collection via go-librespot ("Spill alle" only in
v1 — per-track browsing inside Spotify collections is a later addition).

## 3. Controls (4 buttons: A/B/X/Y on BCM 5, 6, 16, 24)

| Context | X (upper right) | Y (lower right) | A (upper left) | B (lower left) |
|---|---|---|---|---|
| Menu | up | down | select | back |
| Now playing | volume + | volume − | play/pause | back to menu |
| Now playing (hold) | next | prev | — | home |

Exact mapping TBD after kid-testing; the point is that play/pause/next/prev
and **volume** all route through tapboxd (`/playpause /next /prev /volume`),
which the platform already implements — the UI daemon never talks to
go-librespot or mpv directly.

## 4. Audio output: BT headset default, HAT speaker fallback

- **Default:** the bonded BT speaker/headset whenever it is on (existing
  auto-reconnect service handles this).
- **Fallback/local:** the Pirate Audio mini speaker (I2S, ALSA `hifiberry-dac`
  overlay device) — always works, no pairing, good enough for a nightstand.
- Switching (menu item + PWA toggle): mpv takes `--audio-device` per spawn
  (trivial); go-librespot's `audio_device` is startup config, so tapboxd
  rewrites the config and restarts go-librespot on switch (a few seconds,
  acceptable). New daemon endpoint: `POST /output {"device": "bt"|"local"}`.
- Volume works identically on both outputs (mpv softvol / Spotify volume via
  `POST /volume` — implemented 2026-07-05). Note: MAX98357A has no hardware
  volume; software volume is the only path for the HAT speaker.

## 5. GPIO budget (verified compatible)

| Function | Pins |
|---|---|
| Pirate Audio display (SPI0) | BCM 7 (CS), 9 (DC? — MISO unused), 10, 11, 13 (backlight) |
| Pirate Audio I2S audio | BCM 18, 19, 21 |
| Pirate Audio buttons | BCM 5, 6, 16, 24 |
| PiSugar 3 (pogo pins) | I2C (BCM 2, 3) — shared bus |
| PN532 (if reused here) | I2C (BCM 2, 3) — shared bus, different address |
| Card-slot switch (Concept B rig tests) | BCM 17 — free |

No conflicts; I2C is a shared bus (PiSugar 0x57/0x68, PN532 0x24).

## 6. New software (the only real work)

1. **`tapbox-ui` daemon:** ST7789 driver (Pimoroni `st7789` lib) + menu
   renderer + button handling (gpio-keys overlay or gpiozero) + now-playing
   screen (title/position from `GET /status`, battery % from PiSugar).
   Backlight off after N seconds idle (power).
2. **tapboxd additions: ALL DONE (2026-07-06).** `/library` + `/expand` +
   episode play (§2), `/volume`, and `GET/POST /output` — mpv retargets
   live over IPC, go-librespot via config rewrite + service restart;
   "local" maps to ALSA pcm `tapbox_local` (define in asound.conf when
   the HAT arrives; `TAPBOX_LOCAL_PCM` overrides). `/status` carries
   title/artwork/position/duration/episode_id/output. Also implemented:
   offline-aware queue (player.py skips dead stream URLs when offline —
   cached episodes play cleanly; `TAPBOX_OFFLINE=1` forces it).
   The screen UI is now a pure consumer.
3. **PWA:** library editor (add/name/reorder/delete links per service). Same
   editor Concept B needs for card mapping — build once.

Everything else (playback, resume, cache, BT, power) is the existing platform.

## 7. Settings menu (designed 2026-07-06, not built)

Entered via a **parental lock**: hold A+B ~3s (a kid must not be able to
shut the box down mid-story or wipe caches — the lock is the single most
important idea here). Contents:

| Setting | Values | Consumed by |
|---|---|---|
| Screen timeout | 15 / 30 / 60 s / always; **always-on while charging**; any button wakes (the waking press does nothing) | tapbox-ui reads /settings |
| Volume cap (child safety) | 50-100% | tapboxd clamps every /volume path (buttons, AVRCP, phone) |
| Idle auto-shutdown | 15 / 30 / 60 min / off | idle.py re-reads /settings each cycle (no restart) |
| Bluetooth | pair new (auto-pair strongest — validated flow), known-devices list, connect/forget | new /bt endpoints |
| WiFi | on/off + show SSID/IP (the IP doubles as the PWA URL) | /system endpoints |
| Storage | SD used/free, cache sizes per type, clear cache | GET /system |
| Battery | % + charging + **estimated playtime left** (the calibrated curve is literally remaining-playtime — show "~2.5 h igjen") | GET /system |
| Power | shutdown / restart | POST /system/shutdown |
| Later | backlight dimming (PWM on BCM13), language NO/EN, About page | |

**API gaps this requires (playback/menu side is complete):**
1. `GET /system` — battery (pisugar), disk, cache sizes, wifi state, temps, version
2. `POST /system/wifi {"enabled"}`, `POST /system/shutdown`
3. `GET/PUT /settings` — settings.json owned by tapboxd (screen_timeout_s,
   idle_shutdown_min, volume_cap, ...)
4. `/bt` endpoints — move play.sh's pairing/connect logic behind the daemon
   (the consolidation SPEC.md already flags)

## 8. Open questions

- Button layout after kid-testing (3-year-old vs 6-year-old ergonomics differ).
- Should the screen show cover art (Spotify images via the API —
  go-librespot's `server.image_size` picks the delivered size, 240x240ish
  fits our display; NRK psapi has images) or keep a text-only, low-power
  UI in v1?
- Battery impact of the display (backlight ~20-40mA) — measure with the CSV
  logger; auto-off mitigates.
- Does the 1W mini speaker suffice for "nightstand mode", or does Concept A
  also want the Amp SHIM + real driver? (If yes, it converges with Concept B
  hardware and only the input model differs.)
