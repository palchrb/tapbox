#!/usr/bin/env python3
"""TapBox button daemon — generic media-key listener.

Watches EVERY input device that offers media keys: bluetooth AVRCP
buttons (any speaker/headset — BlueZ maps them all to the same standard
evdev codes), USB media keyboards, and future GPIO buttons via the
gpio-keys overlay. Nothing here is specific to one device.

Routing: commands go to the orchestration daemon (which owns "what is
active" and can even resume a dead session); if it is down, fall back to
the direct heuristic (mpv first, else Spotify).

Devices come and go with bluetooth connections, so the device list is
re-scanned every few seconds (hot-plug).
"""

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, "/usr/local/lib/tapbox-py"):
    if os.path.isdir(os.path.join(_p, "tapbox")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from tapbox import boxapi, mpv, spotify  # noqa: E402

# evdev is only needed for the daemon (device watching); the one-shot CLI
# (used by the PiSugar tap shells) imports lazily so it runs on plain python3.

# Standard Linux input-event-codes (raw ints so no evdev import at load)
ACTIONS = {
    200: "playpause",  # KEY_PLAYCD
    201: "playpause",  # KEY_PAUSECD
    164: "playpause",  # KEY_PLAYPAUSE
    166: "playpause",  # KEY_STOPCD
    163: "next",       # KEY_NEXTSONG
    165: "prev",       # KEY_PREVIOUSSONG
    115: "volup",      # KEY_VOLUMEUP
    114: "voldown",    # KEY_VOLUMEDOWN
}
VOL_STEP = 5  # percent per volume key press
# A flaky BT headset (e.g. the JBL AVRCP) can emit rapid self-cancelling
# play/pause bursts; a sub-350ms playpause repeat carries no human intent
# and just churns the A2DP transport (a firmware-crash trigger on the Zero
# 2 W). next/prev/volume repeats ARE legitimate, so only playpause debounces.
REPEAT_DEBOUNCE_S = 0.35
MPV_CMDS = {
    "playpause": ["cycle", "pause"],
    "next": ["playlist-next"],
    "prev": ["playlist-prev"],
    "volup": ["add", "volume", VOL_STEP],
    "voldown": ["add", "volume", -VOL_STEP],
}


def log(msg):
    print(f"buttons: {msg}", flush=True)


def spotify_command(action):
    if action in ("volup", "voldown"):
        steps = spotify.status().get("volume_steps") or 65535
        delta = VOL_STEP if action == "volup" else -VOL_STEP
        spotify.go("/player/volume",
                   body={"volume": round(delta * steps / 100), "relative": True})
    else:
        spotify.command(action)


def handle(action):
    # Preferred: the orchestration daemon owns "what is active" and can
    # even resume the last-played target on a dead session.
    try:
        if action in ("volup", "voldown"):
            path = "/volume"
            body = {"delta": VOL_STEP if action == "volup" else -VOL_STEP}
        else:
            path, body = "/" + action, {}
        routed = boxapi.post(path, body).get("routed")
        log(f"{action} -> daemon ({routed})")
        return
    except (OSError, ValueError):
        pass  # daemon not running — fall back to direct heuristic
    try:
        if mpv.ipc(MPV_CMDS[action]).get("error") == "success":
            log(f"{action} -> mpv")
            return
    except OSError:
        pass  # no mpv running — try spotify
    try:
        spotify_command(action)
        log(f"{action} -> spotify")
    except OSError as e:
        log(f"{action}: no active player ({e})")


def rescan(devs):
    """Open new media-key devices, drop vanished ones."""
    from evdev import InputDevice, ecodes, list_devices
    for path in list_devices():
        if path in devs:
            continue
        try:
            dev = InputDevice(path)
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            if any(code in keys for code in ACTIONS):
                log(f"watching {dev.name} ({path})")
                devs[path] = dev
            else:
                dev.close()
        except OSError:
            pass
    for path in list(devs):
        if not os.path.exists(path):
            log(f"lost {devs[path].name}")
            try:
                devs[path].close()
            except OSError:
                pass
            devs.pop(path)


def main():
    from evdev import ecodes
    from select import select
    devs = {}
    last_scan = 0.0
    last_fired = {}          # action -> monotonic time, for repeat debounce
    log("started — waiting for media-key devices (AVRCP, keyboards, ...)")
    while True:
        if time.time() - last_scan > 5:
            last_scan = time.time()
            rescan(devs)
        if not devs:
            time.sleep(2)
            continue
        readable, _, _ = select(list(devs.values()), [], [], 5)
        for dev in readable:
            try:
                for ev in dev.read():
                    if (ev.type == ecodes.EV_KEY and ev.value == 1
                            and ev.code in ACTIONS):
                        action = ACTIONS[ev.code]
                        # drop a flaky peer's sub-350ms play/pause repeat
                        # (see REPEAT_DEBOUNCE_S) — no human intent, and it's
                        # what churns the A2DP transport
                        if action == "playpause":
                            now = time.monotonic()
                            if now - last_fired.get(action, 0.0) \
                                    < REPEAT_DEBOUNCE_S:
                                continue
                            last_fired[action] = now
                        handle(action)
            except OSError:  # device disconnected mid-read
                devs.pop(dev.path, None)


if __name__ == "__main__":
    # One-shot CLI (used by the PiSugar tap shells): `buttons.py next`
    if len(sys.argv) > 1:
        action = sys.argv[1]
        if action not in ("playpause", "next", "prev", "volup", "voldown"):
            print("usage: buttons.py [playpause|next|prev|volup|voldown]",
                  file=sys.stderr)
            sys.exit(1)
        handle(action)
    else:
        main()
