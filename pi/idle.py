#!/usr/bin/env python3
"""TapBox idle auto-shutdown (opt-in, installed as tapbox-idle).

Powers the box off after IDLE_MIN minutes without playback, to save
battery when it's been left on and forgotten. The PiSugar's physical
button powers it back on (cold boot ~25-35s).

"Playing" means go-librespot is actively playing (Spotify) OR mpv is
running and not paused. A paused player counts as idle, so pausing and
walking away eventually shuts down too.

Usage: idle.py [minutes]   (default 30; enable via `tapbox-power idle-on`)
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

API = "http://127.0.0.1:3678"
DAEMON = "http://127.0.0.1:3679"
MPV_SOCK = "/run/tapbox-mpv.sock"
SETTINGS_FILE = os.environ.get("TAPBOX_SETTINGS", "/etc/tapbox/settings.json")
IDLE_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 30
CHECK_S = 60


def idle_minutes():
    """The configured timeout: tapboxd's settings.json wins (re-read every
    cycle so the settings menu applies live); the CLI arg is the fallback.
    0 = auto-shutdown disabled."""
    try:
        with open(SETTINGS_FILE) as f:
            return int(json.load(f)["idle_shutdown_min"])
    except (OSError, ValueError, KeyError, TypeError):
        return IDLE_MIN


def go_playing():
    try:
        with urllib.request.urlopen(API + "/status", timeout=5) as r:
            s = json.loads(r.read())
        return bool(s.get("track")) and not s.get("paused") and not s.get("stopped")
    except (OSError, ValueError):
        return False


def mpv_playing():
    try:
        with socket.socket(socket.AF_UNIX) as c:
            c.settimeout(2)
            c.connect(MPV_SOCK)
            c.sendall(b'{"command":["get_property","pause"]}\n')
            for line in c.recv(65536).split(b"\n"):
                if not line.strip():
                    continue
                m = json.loads(line)
                if "error" in m:
                    return m.get("error") == "success" and m.get("data") is False
    except (OSError, ValueError):
        return False
    return False


def daemon_playing():
    """Unified answer from the orchestration daemon, None if it's down."""
    try:
        with urllib.request.urlopen(DAEMON + "/status", timeout=5) as r:
            return bool(json.loads(r.read()).get("playing"))
    except (OSError, ValueError):
        return None


def main():
    idle = 0
    print(f"tapbox-idle: will power off after {idle_minutes()} min without "
          "playback (live from settings.json)", flush=True)
    while True:
        active = daemon_playing()
        if active is None:  # daemon down — check the sources directly
            active = go_playing() or mpv_playing()
        idle = 0 if active else idle + CHECK_S
        limit = idle_minutes()
        if limit > 0 and idle >= limit * 60:
            subprocess.run(["logger",
                            f"tapbox-idle: idle {limit}min, powering off"])
            subprocess.run(["poweroff"])
            return
        time.sleep(CHECK_S)


if __name__ == "__main__":
    main()
