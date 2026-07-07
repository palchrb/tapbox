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

## Setup

Everything on-box installs and updates with one idempotent script:

```
git clone <this repo> ~/tunebox
cd ~/tunebox && sudo ./pi/install.sh     # update later: git pull && sudo ./pi/install.sh
```

### Outside install.sh (by design)

| What | How | Why manual |
|---|---|---|
| OS basics | Raspberry Pi Imager (hostname, user, wifi, SSH) | pre-boot |
| pisugar-server | PiSugar's own installer script | third-party installer; install.sh only patches its config (battery curve) when present |
| PiSugar safe-shutdown / taps | PiSugar web UI (:8421) or `tapbox-power taps-on` | user preference |
| Tailscale (optional, remote admin) | official installer + `tailscale up` | interactive auth |
| `maxcpus=2` in cmdline.txt (optional) | manual edit | kernel has no CPU hotplug; only for max battery |
| Power-save at boot (optional) | `sudo tapbox-power boot-on` | opt-in trade-off |
| Pirate Audio HAT (when mounted) | `sudo tapbox-power hat-audio-on` + reboot + enable `tapbox-ui` | hardware-gated |
| PN532 RFID (when wired) | `sudo systemctl enable --now tapbox-rfid` | hardware-gated |
| Spotify login | pick the box under Devices in the Spotify app (same wifi) | zeroconf by design |
| BT speaker pairing | PWA settings -> Bluetooth (or screen) | per-home config |

## Why this exists

Today's kid-friendly speakers (Tonies, Yoto) sell hardware cheaply and recoup margin via proprietary content figurines/cards. Parents end up paying twice for content they already own through Spotify/Apple Music/etc.

tapbox flips that: bring your own music subscriptions, control playback with reusable RFID/NFC cards, and own the device end-to-end.

## License

TBD. Likely Apache 2.0 (aligns with the Music Assistant dependency) or dual-licensed for commercial distribution. Not yet committed.
