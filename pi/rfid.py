#!/usr/bin/env python3
"""TapBox RFID daemon — tap a card, play its mapped content.

Reads a PN532 NFC module over I2C. Card UIDs map to targets in
/etc/tapbox/cards.json. A target is either:
  - a Spotify link/URI (track/album/playlist/artist/episode/show)
      -> played via go-librespot's HTTP API
  - anything else: a URL (NRK program pages etc. resolved by mpv+yt-dlp,
    or a direct stream) or a local file path
      -> played via mpv straight to the bluetooth headset

Mapping a card ("learn mode"): card.sh writes the target to
/etc/tapbox/pending-map; the next tapped card is bound to it and plays.

Power notes: the read loop polls ~2x/second (short read timeout + sleep),
which keeps the PN532 and CPU mostly idle — this is the "power efficiency
as part of the design" baseline. A future refinement is wiring the PN532
IRQ pin to a GPIO and sleeping until a card wakes us.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request

API = "http://127.0.0.1:3678"
CARDS_FILE = "/etc/tapbox/cards.json"
PENDING_FILE = "/etc/tapbox/pending-map"
ALSA_DEVICE = "alsa/tapbox_bt"  # same output go-librespot uses
READ_TIMEOUT_S = 0.15  # how long each poll waits for a card
POLL_SLEEP_S = 0.4     # idle time between polls (power)
DEBOUNCE_S = 3.0       # ignore same card while it rests on the reader

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("tapbox-rfid")

SPOTIFY_URI_RE = re.compile(
    r"^spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]+$")
SPOTIFY_LINK_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z-]+/)?"
    r"(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)")

mpv_proc = None


def api(path, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def to_spotify_uri(target):
    if SPOTIFY_URI_RE.match(target):
        return target
    if "spotify.link/" in target:  # short links redirect to open.spotify.com
        try:
            with urllib.request.urlopen(target, timeout=10) as resp:
                target = resp.url
        except OSError as e:
            log.warning("could not resolve short link %s: %s", target, e)
            return None
    m = SPOTIFY_LINK_RE.search(target)
    if m:
        return f"spotify:{m.group(1)}:{m.group(2)}"
    return None


def stop_mpv():
    global mpv_proc
    if mpv_proc and mpv_proc.poll() is None:
        mpv_proc.terminate()
        try:
            # player.py forwards SIGTERM to mpv and exits after its poll
            # cycle — give it time so mpv is never left orphaned playing
            mpv_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mpv_proc.kill()
    mpv_proc = None


def play(target):
    global mpv_proc
    uri = to_spotify_uri(target)
    stop_mpv()
    if uri:
        log.info("playing via spotify: %s", uri)
        api("/player/play", {"uri": uri})
        return
    log.info("playing via mpv: %s", target)
    try:
        import nrk
        urls = nrk.expand(target)
        if urls != [target]:
            log.info("expanded to %d stream(s)", len(urls))
    except Exception as e:
        log.warning("nrk expansion failed (%s), passing link straight to mpv", e)
        urls = [target]
    try:
        api("/player/pause", {})
    except OSError:
        pass  # spotify daemon not up or nothing playing — fine
    player = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player.py")
    if not os.path.exists(player):
        player = "/usr/local/bin/tapbox-player"
    mpv_proc = subprocess.Popen([sys.executable, player, target] + urls)
    # Cache the newest episodes in the background for offline playback
    try:
        import nrk
        slug = nrk.podcast_slug(target)
        if slug:
            subprocess.Popen(
                [sys.executable, nrk.__file__, "sync", slug],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning("could not start podcast sync: %s", e)


def load_cards():
    try:
        with open(CARDS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cards(cards):
    os.makedirs(os.path.dirname(CARDS_FILE), exist_ok=True)
    tmp = CARDS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cards, f, indent=2)
    os.replace(tmp, CARDS_FILE)


def handle_tap(uid):
    cards = load_cards()
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            target = f.read().strip()
        os.remove(PENDING_FILE)
        if target:
            cards[uid] = target
            save_cards(cards)
            log.info("mapped card %s -> %s", uid, target)
            play(target)
            return
    if uid in cards:
        play(cards[uid])
    else:
        log.info("unknown card %s — map it with: sudo card.sh map <link>", uid)


def main():
    try:
        import board
        import busio
        from adafruit_pn532.i2c import PN532_I2C
        i2c = busio.I2C(board.SCL, board.SDA)
        pn532 = PN532_I2C(i2c, debug=False)
        ic, ver, rev, _ = pn532.firmware_version
        pn532.SAM_configuration()
    except Exception as e:  # no reader, i2c disabled, lib missing, ...
        log.error("PN532 init failed (%s) — is the module wired and I2C "
                  "enabled? Retrying in 60s.", e)
        time.sleep(60)
        sys.exit(1)  # systemd restarts us

    log.info("PN532 ready (firmware %d.%d), polling for cards", ver, rev)
    last_uid, last_seen = None, 0.0
    errors = 0
    while True:
        try:
            uid = pn532.read_passive_target(timeout=READ_TIMEOUT_S)
            errors = 0
        except Exception as e:
            errors += 1
            if errors > 10:
                log.error("repeated reader errors (%s), restarting", e)
                sys.exit(1)
            time.sleep(1)
            continue

        now = time.monotonic()
        if uid is not None:
            uid_hex = uid.hex()
            if uid_hex != last_uid or now - last_seen >= DEBOUNCE_S:
                log.info("card tapped: %s", uid_hex)
                try:
                    handle_tap(uid_hex)
                except Exception as e:
                    log.error("tap handling failed: %s", e)
            last_uid, last_seen = uid_hex, now
        time.sleep(POLL_SLEEP_S)


if __name__ == "__main__":
    main()
