#!/usr/bin/env python3
"""TapBox RFID daemon — tap a card, play its mapped content.

Reads a PN532 NFC module over I2C. Card UIDs map to targets in
/etc/tapbox/cards.json. All link routing lives in player.py (Spotify ->
go-librespot API; NRK/RSS/streams/files -> nrk.py expansion + mpv with
resume); this daemon just hands the target over.

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
import subprocess
import sys
import time

CARDS_FILE = "/etc/tapbox/cards.json"
PENDING_FILE = "/etc/tapbox/pending-map"
READ_TIMEOUT_S = 0.15   # how long each poll waits for a card
POLL_SLEEP_S = 0.4      # pause between polls while the box is in use
IDLE_AFTER_S = 180      # no taps for this long -> slow polling
IDLE_POLL_SLEEP_S = 1.0 # pause between polls when idle (max tap latency)
DEBOUNCE_S = 3.0        # ignore same card while it rests on the reader

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("tapbox-rfid")

mpv_proc = None


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
    stop_mpv()
    player = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player.py")
    if not os.path.exists(player):
        player = "/usr/local/bin/tapbox-player"
    log.info("playing: %s", target)
    # player.py routes: Spotify -> go-librespot API (exits right away),
    # everything else -> mpv with resume (runs until stopped/finished)
    mpv_proc = subprocess.Popen([sys.executable, player, target])


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

    # Power: the RF field burns the power, so between polls the PN532 is
    # put in its power-down state (~uA; the adafruit driver wakes it on the
    # next command), and polling slows down after a few tap-less minutes.
    # Next step when wired: PN532 IRQ pin -> GPIO for interrupt wake.
    can_power_down = hasattr(pn532, "power_down")
    log.info("PN532 ready (firmware %d.%d), polling for cards%s",
             ver, rev, " (power-down between polls)" if can_power_down else "")
    last_uid, last_seen = None, 0.0
    last_tap = time.monotonic()
    errors = 0
    while True:
        try:
            uid = pn532.read_passive_target(timeout=READ_TIMEOUT_S)
            if can_power_down:
                try:
                    pn532.power_down()
                except Exception:
                    can_power_down = False  # not supported by this firmware
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
            last_tap = now
        idle = now - last_tap > IDLE_AFTER_S
        time.sleep(IDLE_POLL_SLEEP_S if idle else POLL_SLEEP_S)


if __name__ == "__main__":
    main()
