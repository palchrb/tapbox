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
import time
import urllib.request
from select import select

from evdev import InputDevice, ecodes, list_devices

API = "http://127.0.0.1:3678"
MPV_SOCK = os.environ.get("TAPBOX_MPV_SOCK", "/run/tapbox-mpv.sock")

ACTIONS = {
    ecodes.KEY_PLAYCD: "playpause",
    ecodes.KEY_PAUSECD: "playpause",
    ecodes.KEY_PLAYPAUSE: "playpause",
    ecodes.KEY_STOPCD: "playpause",
    ecodes.KEY_NEXTSONG: "next",
    ecodes.KEY_PREVIOUSSONG: "prev",
}
MPV_CMDS = {
    "playpause": ["cycle", "pause"],
    "next": ["playlist-next"],
    "prev": ["playlist-prev"],
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


def spotify_command(action):
    req = urllib.request.Request(
        API + SPOTIFY_PATHS[action], data=b"{}",
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def handle(action):
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
    main()
