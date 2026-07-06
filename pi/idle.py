#!/usr/bin/env python3
"""TapBox idle auto-shutdown (opt-in, installed as tapbox-idle).

Powers the box off after N minutes without playback, to save battery
when it's been left on and forgotten. The PiSugar's physical button
powers it back on (cold boot ~25-35s).

"Playing" means go-librespot is actively playing (Spotify) OR mpv is
running and not paused. A paused player counts as idle, so pausing and
walking away eventually shuts down too.

The timeout comes from tapboxd's settings.json (idle_shutdown_min,
re-read every cycle so the settings menu applies live; 0 = disabled);
the CLI argument is the fallback when no settings file exists.

Usage: idle.py [minutes]   (default 30; enable via `tapbox-power idle-on`)
"""

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
from tapbox.paths import read_settings  # noqa: E402

IDLE_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 30
CHECK_S = 60


def idle_minutes():
    v = read_settings().get("idle_shutdown_min")
    return int(v) if isinstance(v, (int, float)) else IDLE_MIN


def daemon_playing():
    """Unified answer from the orchestration daemon, None if it's down."""
    try:
        return bool(boxapi.get("/status", timeout=5).get("playing"))
    except (OSError, ValueError):
        return None


def main():
    idle = 0
    print(f"tapbox-idle: will power off after {idle_minutes()} min without "
          "playback (live from settings.json)", flush=True)
    while True:
        active = daemon_playing()
        if active is None:  # daemon down — check the sources directly
            active = spotify.playing() or mpv.playing()
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
