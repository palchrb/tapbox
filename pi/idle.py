#!/usr/bin/env python3
"""TapBox idle auto-shutdown (installed as tapbox-idle, on by default).

Powers the box off after N minutes without ACTIVITY, to save battery
when it's been left on and forgotten. The PiSugar's physical button
powers it back on (cold boot ~25-35s).

Activity is either of:
  - playback: go-librespot actively playing (Spotify) OR mpv running
    and not paused. A paused player counts as idle, so pausing and
    walking away eventually shuts down too.
  - hands on the box: the UI touches an advisory marker on every
    button press (paths.touch_activity), so a kid browsing the
    carousel without starting anything never has the box die mid-use.

The timeout comes from tapboxd's settings.json (idle_shutdown_min,
re-read every cycle so the settings menu applies live; 0 = disabled).
While disabled NOTHING accumulates — flipping the setting back on
starts a fresh countdown instead of powering off within the minute.
The CLI argument is the fallback when no settings file exists.

Usage: idle.py [minutes]   (default 5)
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
from tapbox.paths import last_activity, read_settings  # noqa: E402

IDLE_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 5
CHECK_S = 60
# A button press younger than this counts as 'in use'. Two check
# periods: a press can land anywhere between samples, and one extra
# cycle of grace is cheaper than a box dying in a kid's hands.
ACTIVITY_FRESH_S = CHECK_S * 2


def idle_minutes():
    v = read_settings().get("idle_shutdown_min")
    return int(v) if isinstance(v, (int, float)) else IDLE_MIN


def daemon_playing():
    """Unified answer from the orchestration daemon, None if it's down."""
    try:
        return bool(boxapi.get("/status", timeout=5).get("playing"))
    except (OSError, ValueError):
        return None


def _cycle(idle):
    """One check: the new idle-seconds count, or None after poweroff."""
    active = daemon_playing()
    if active is None:  # daemon down — check the sources directly
        active = spotify.playing() or mpv.playing()
    if not active:
        age = time.time() - last_activity()
        # A negative age is a clock jump (boot RTC/NTP) — same as the
        # radio markers, treat it as no signal rather than fresh.
        if 0 <= age < ACTIVITY_FRESH_S:
            active = True  # someone is pressing buttons — in use
    idle = 0 if active else idle + CHECK_S
    limit = idle_minutes()
    if limit <= 0:
        return 0  # disabled: never accumulate behind the parent's back
    if idle >= limit * 60:
        subprocess.run(["logger",
                        f"tapbox-idle: idle {limit}min, powering off"])
        subprocess.run(["poweroff"])
        return None
    return idle


def main():
    idle = 0
    print(f"tapbox-idle: will power off after {idle_minutes()} min without "
          "playback or button presses (live from settings.json)", flush=True)
    while True:
        idle = _cycle(idle)
        if idle is None:
            return
        time.sleep(CHECK_S)


if __name__ == "__main__":
    main()
