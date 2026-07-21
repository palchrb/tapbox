#!/usr/bin/env python3
"""pipod Hold-switch daemon — DRAFT (untested).

The iPod Hold switch is a latching SPDT slide (RESEARCH §4). We read it as a
stable LEVEL on a GPIO (not the edge-triggered dtoverlay=gpio-shutdown,
which fights a latching switch):

  Hold engaged  -> write LOCK_FILE  (podui.py ignores the wheel, dims screen)
  engaged > HOLD_SHUTDOWN_S -> `sudo poweroff` (PiSugar safe-shutdown cuts
                               power). Power-on is PiSugar's own button.
  Hold released -> remove LOCK_FILE

Wiring (see HARDWARE.md): COM -> BCM17, one throw -> GND, other -> 3V3.
Adjust ACTIVE_STATE after metering which position is "locked".
"""

import os
import subprocess
import time

PIN = int(os.environ.get("PIPOD_HOLD_PIN", "17"))
LOCK_FILE = os.environ.get("PIPOD_LOCK", "/run/pipod-hold.lock")
HOLD_SHUTDOWN_S = float(os.environ.get("PIPOD_HOLD_SHUTDOWN_S", "4"))
# Which switch level means "locked". Meter your wheel and flip if needed.
ACTIVE_STATE = os.environ.get("PIPOD_HOLD_ACTIVE", "1") == "1"


def log(msg):
    print(f"holdswitch: {msg}", flush=True)


def set_lock(on):
    try:
        if on:
            open(LOCK_FILE, "w").close()
        elif os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError as e:
        log(f"lock file: {e}")


def main():
    from gpiozero import DigitalInputDevice
    # pull to the inactive rail so a floating line reads "unlocked"
    dev = DigitalInputDevice(PIN, pull_up=not ACTIVE_STATE)
    log(f"watching Hold on BCM{PIN} (active={int(ACTIVE_STATE)})")

    engaged_since = None
    was = None
    while True:
        engaged = (dev.value == 1) == ACTIVE_STATE
        if engaged != was:
            set_lock(engaged)
            log("LOCK" if engaged else "unlock")
            engaged_since = time.time() if engaged else None
            was = engaged
        if engaged and engaged_since and (time.time() - engaged_since) >= HOLD_SHUTDOWN_S:
            log(f"held {HOLD_SHUTDOWN_S}s -> poweroff")
            subprocess.run(["sudo", "poweroff"])
            return
        time.sleep(0.1)


if __name__ == "__main__":
    main()
