#!/usr/bin/env python3
"""TapBox button daemon — generic media-key listener.

Watches EVERY input device that offers media keys: bluetooth AVRCP
buttons (any speaker/headset — BlueZ maps them all to the same standard
evdev codes), USB media keyboards, and future GPIO buttons via the
gpio-keys overlay. Nothing here is specific to one device.

Routing (same rule as everywhere else in tapbox): if the mpv player is
running (its IPC socket answers), commands go there; otherwise to
go-librespot's API (Spotify).

Devices come and go with bluetooth connections, so the device list is
re-scanned every few seconds (hot-plug).
"""

import json
import os
import socket
import sys
import time
import urllib.request

# evdev is only needed for the daemon (device watching); the one-shot CLI
# (used by the PiSugar tap shells) imports lazily so it runs on plain python3.

API = "http://127.0.0.1:3678"
DAEMON = "http://127.0.0.1:3679"
MPV_SOCK = os.environ.get("TAPBOX_MPV_SOCK", "/run/tapbox-mpv.sock")

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
MPV_CMDS = {
    "playpause": ["cycle", "pause"],
    "next": ["playlist-next"],
    "prev": ["playlist-prev"],
    "volup": ["add", "volume", VOL_STEP],
    "voldown": ["add", "volume", -VOL_STEP],
}
SPOTIFY_PATHS = {
    "playpause": "/player/playpause",
    "next": "/player/next",
    "prev": "/player/prev",
}


def log(msg):
    print(f"buttons: {msg}", flush=True)


def mpv_command(cmd):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(2)
        s.connect(MPV_SOCK)
        s.sendall(json.dumps({"command": cmd}).encode() + b"\n")
        resp = json.loads(s.recv(65536).split(b"\n")[0])
        return resp.get("error") == "success"


def spotify_post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        API + path, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def spotify_status():
    try:
        with urllib.request.urlopen(API + "/status", timeout=5) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return {}


def spotify_command(action):
    if action in ("volup", "voldown"):
        steps = spotify_status().get("volume_steps") or 65535
        delta = VOL_STEP if action == "volup" else -VOL_STEP
        spotify_post("/player/volume",
                     {"volume": round(delta * steps / 100), "relative": True})
        return
    if action != "prev":
        spotify_post(SPOTIFY_PATHS[action])
        return
    # "prev" in Spotify rewinds the current track first and only jumps to the
    # previous track on a second press. Since the button is one gesture, do
    # the second press ourselves when the first only rewound.
    before = spotify_status().get("track", {}).get("uri")
    spotify_post("/player/prev")
    time.sleep(0.4)
    after = spotify_status()
    same = after.get("track", {}).get("uri") == before
    if same and (after.get("position") or 0) < 2000:
        spotify_post("/player/prev")  # it only rewound — go to the real prev


def handle(action):
    # Preferred: the orchestration daemon owns "what is active" and can
    # even resume the last-played target on a dead session.
    try:
        if action in ("volup", "voldown"):
            path, data = "/volume", json.dumps(
                {"delta": VOL_STEP if action == "volup" else -VOL_STEP}).encode()
        else:
            path, data = "/" + action, b"{}"
        req = urllib.request.Request(
            DAEMON + path, data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            routed = json.loads(r.read()).get("routed")
        log(f"{action} -> daemon ({routed})")
        return
    except (OSError, ValueError):
        pass  # daemon not running — fall back to direct heuristic
    try:
        if mpv_command(MPV_CMDS[action]):
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
                        handle(ACTIONS[ev.code])
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
