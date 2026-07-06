#!/usr/bin/env python3
"""TapBox RFID daemon — a card starts its mapped content.

Two modes, selected via /etc/tapbox/rfid.conf (systemd EnvironmentFile):

  Poll mode (default, no config needed): the PN532 polls ~2x/s over I2C
    with power-down between polls. Tap a card -> play. This is the
    no-extra-wiring baseline.

  Slot mode (SLOT_GPIO=<bcm pin>): a detector switch in the card slot is
    the presence sensor (one pole to GND, one to the GPIO; internal
    pull-up). The PN532 stays untouched — and can be fully power-gated
    via PN532_POWER_GPIO — until the switch says a card arrived; then it
    is read ONCE and released. Card in slot = playing, card removed =
    paused (the daemon keeps the player loaded, so re-inserting the same
    card unpauses instantly; a different card switches content). The RF
    field runs ~100ms per card change instead of all day.

    For testing without a switch (or without the PN532):
      SLOT_GPIO=file:/tmp/card   -> `touch /tmp/card` = insert, rm = remove
      FAKE_UID=cafebabe          -> UID used when no reader answers

Card UIDs map to targets in /etc/tapbox/cards.json. All link routing
lives in player.py behind the orchestration daemon; this daemon just
hands targets over. Mapping a card ("learn mode"): card.sh writes the
target to /etc/tapbox/pending-map; the next card is bound to it.
"""

import json
import logging
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, "/usr/local/lib/tapbox-py"):
    if os.path.isdir(os.path.join(_p, "tapbox")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from tapbox import boxapi, mpv, spotify  # noqa: E402

CARDS_FILE = "/etc/tapbox/cards.json"
PENDING_FILE = "/etc/tapbox/pending-map"
READ_TIMEOUT_S = 0.15   # how long each poll waits for a card (poll mode)
POLL_SLEEP_S = 0.4      # pause between polls while the box is in use
IDLE_AFTER_S = 180      # no taps for this long -> slow polling
IDLE_POLL_SLEEP_S = 1.0 # pause between polls when idle (max tap latency)
DEBOUNCE_S = 3.0        # ignore same card while it rests on the reader
SLOT_SAMPLE_S = 0.1     # switch sample interval (slot mode)
SLOT_STABLE_N = 3       # samples the switch must hold before we act
SLOT_READ_S = 3.0       # how long to try reading a freshly inserted card

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
    log.info("playing: %s", target)
    # Preferred path: hand the target to the orchestration daemon, which
    # owns playback state (so buttons etc. route coherently afterwards).
    try:
        boxapi.post("/play", {"target": target})
        return
    except (OSError, ValueError) as e:
        log.warning("daemon unreachable (%s) — playing directly", e)
    stop_mpv()
    player = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player.py")
    if not os.path.exists(player):
        player = "/usr/local/bin/tapbox-player"
    mpv_proc = subprocess.Popen([sys.executable, player, target])


def pause():
    """Pause whatever is audible (card removed from the slot)."""
    try:
        boxapi.post("/pause", {}, timeout=10)
        return
    except (OSError, ValueError) as e:
        log.warning("daemon unreachable (%s) — pausing directly", e)
    try:
        mpv.ipc(["set_property", "pause", True])
    except OSError:
        pass
    try:
        spotify.go("/player/pause")
    except OSError:
        pass


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


# --- PN532 --------------------------------------------------------------------

def init_reader():
    import board
    import busio
    from adafruit_pn532.i2c import PN532_I2C
    i2c = busio.I2C(board.SCL, board.SDA)
    pn532 = PN532_I2C(i2c, debug=False)
    ic, ver, rev, _ = pn532.firmware_version
    pn532.SAM_configuration()
    return pn532, ver, rev


# --- slot mode ------------------------------------------------------------------

def open_power_gate(bcm):
    """Optional MOSFET gate on the PN532's supply: high = powered."""
    import board
    import digitalio
    p = digitalio.DigitalInOut(getattr(board, f"D{bcm}"))
    p.direction = digitalio.Direction.OUTPUT
    p.value = False
    return p


def make_presence_probe(spec, present_low):
    """Return (probe_fn, description). spec is a BCM pin number, or
    'file:<path>' for testing (file exists = card present)."""
    if spec.startswith("file:"):
        path = spec[5:]
        return (lambda: os.path.exists(path)), f"test file {path}"
    import board
    import digitalio
    pin = digitalio.DigitalInOut(getattr(board, f"D{int(spec)}"))
    pin.direction = digitalio.Direction.INPUT
    pin.pull = digitalio.Pull.UP
    if present_low:
        return (lambda: not pin.value), f"GPIO{spec} (active low)"
    return (lambda: pin.value), f"GPIO{spec} (active high)"


def read_uid_once(gate, fake_uid):
    """Power the reader, read a single UID, release the reader.
    The slot aligns the card over the antenna, so this normally succeeds
    on the first attempt; we retry for up to SLOT_READ_S anyway."""
    pn532 = None
    if gate is not None:
        gate.value = True
        time.sleep(0.1)  # PN532 boot after power-up
    try:
        try:
            pn532, ver, rev = init_reader()
        except Exception as e:
            if fake_uid:
                log.warning("no PN532 (%s) — using FAKE_UID", e)
                return fake_uid
            log.error("card inserted but PN532 init failed: %s", e)
            return None
        deadline = time.monotonic() + SLOT_READ_S
        while time.monotonic() < deadline:
            try:
                uid = pn532.read_passive_target(timeout=0.2)
            except Exception:
                uid = None
            if uid is not None:
                return uid.hex()
        return fake_uid  # None unless testing
    finally:
        if gate is not None:
            gate.value = False  # hard off beats any power-down mode
        elif pn532 is not None:
            try:
                pn532.power_down()
            except Exception:
                pass


def main_slot():
    spec = os.environ["SLOT_GPIO"]
    present_low = os.environ.get("SLOT_PRESENT", "low").lower() != "high"
    fake_uid = os.environ.get("FAKE_UID") or None
    try:
        card_present, desc = make_presence_probe(spec, present_low)
    except Exception as e:
        log.error("cannot open slot sensor %s (%s). Retrying in 60s.", spec, e)
        time.sleep(60)
        sys.exit(1)  # systemd restarts us
    gate = None
    gate_bcm = os.environ.get("PN532_POWER_GPIO")
    if gate_bcm:
        try:
            gate = open_power_gate(int(gate_bcm))
        except Exception as e:
            log.warning("PN532 power gate GPIO%s unavailable (%s) — "
                        "running ungated", gate_bcm, e)

    def on_insert():
        uid = read_uid_once(gate, fake_uid)
        if uid:
            log.info("card inserted: %s", uid)
            try:
                handle_tap(uid)
            except Exception as e:
                log.error("tap handling failed: %s", e)
        else:
            log.warning("card inserted but no UID could be read")

    present = card_present()
    log.info("slot mode: sensor %s%s%s — card %s at start", desc,
             ", PN532 power-gated" if gate else "",
             f", FAKE_UID={fake_uid}" if fake_uid else "",
             "present" if present else "absent")
    if present:
        on_insert()  # booted with a card in the slot -> play it

    stable = 0
    while True:
        time.sleep(SLOT_SAMPLE_S)
        v = card_present()
        if v == present:
            stable = 0
            continue
        stable += 1
        if stable < SLOT_STABLE_N:  # debounce the mechanical switch
            continue
        present, stable = v, 0
        if present:
            on_insert()
        else:
            log.info("card removed — pausing")
            pause()


# --- poll mode (no slot switch wired) -------------------------------------------

def main_poll():
    try:
        pn532, ver, rev = init_reader()
    except Exception as e:  # no reader, i2c disabled, lib missing, ...
        log.error("PN532 init failed (%s) — is the module wired and I2C "
                  "enabled? Retrying in 60s.", e)
        time.sleep(60)
        sys.exit(1)  # systemd restarts us

    # Power: the RF field burns the power, so between polls the PN532 is
    # put in its power-down state (~uA; the adafruit driver wakes it on the
    # next command), and polling slows down after a few tap-less minutes.
    # The real fix is slot mode (see module docstring) once a card-slot
    # detector switch is wired.
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


def main():
    if os.environ.get("SLOT_GPIO"):
        main_slot()
    else:
        main_poll()


if __name__ == "__main__":
    main()
