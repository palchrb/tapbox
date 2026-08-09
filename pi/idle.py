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
from tapbox import boxapi, mpv, renderer, spotify  # noqa: E402
from tapbox.paths import last_activity, read_settings  # noqa: E402

IDLE_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 5
CHECK_S = 60


def describe(minutes):
    """'0' means DISABLED — say so. The old line printed 'will power
    off after 0 min', which read as an instant-shutdown bug and sent
    the owner hunting in the wrong daemon (field 2026-07-29: the real
    culprit was a pisugar-server tap shell)."""
    if minutes <= 0:
        return "idle auto-shutdown DISABLED (idle_shutdown_min=0)"
    return (f"will power off after {minutes} min without playback or "
            "button presses")
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


def sonos_playing():
    """Third direct probe for the daemon-down window: a Sonos rendering
    OUR session must hold auto-off — powering the box off kills the
    controller and the bookmark while the music plays on in the corner
    (QA review 2026-08-09; the round-1 'idle needs zero changes' was
    only true with tapboxd up). Only a CONFIRMED playing state holds:
    sidecar down or stale answers False, or a dead sidecar would pin
    the box awake forever."""
    if not renderer.is_sonos():
        return False
    try:
        snap = renderer.get("/state", timeout=3)
    except (OSError, ValueError):
        return False
    stale = snap.get("stale_s")
    return (snap.get("transport") == "PLAYING" and snap.get("ours")
            and stale is not None and stale < 30)


def ssh_active():
    """Anyone logged in over ssh? An active session means a human is
    working ON the box — powering off under them cost a debugging
    evening (field 2026-08-03: the 5-min idle fired mid-journalctl,
    and the wedged pisugar poweroff then needed a hard cut). utmp is
    gone on trixie (systemd built with -UTMP), so `who` is blind —
    count established TCP sessions on the sshd port instead. Errors
    mean 'unknown': fail toward the OLD behavior (shutdown proceeds),
    never toward a box that can't sleep because a probe broke."""
    try:
        out = subprocess.run(
            ["ss", "-Htn", "state", "established", "( sport = :22 )"],
            capture_output=True, text=True, timeout=5).stdout
        return bool(out and out.strip())
    except (OSError, subprocess.TimeoutExpired, AttributeError):
        return False


def _cycle(idle):
    """One check: the new idle-seconds count, or None after poweroff."""
    active = daemon_playing()
    if active is None:  # daemon down — check the sources directly
        active = spotify.playing() or mpv.playing() or sonos_playing()
    if not active:
        age = time.time() - last_activity()
        # A negative age is a clock jump (boot RTC/NTP) — same as the
        # radio markers, treat it as no signal rather than fresh.
        if 0 <= age < ACTIVITY_FRESH_S:
            active = True  # someone is pressing buttons — in use
    if not active and ssh_active():
        active = True  # a human is on the box over ssh — hold auto-off
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
    print(f"tapbox-idle: {describe(idle_minutes())} "
          "(live from settings.json)", flush=True)
    while True:
        idle = _cycle(idle)
        if idle is None:
            return
        time.sleep(CHECK_S)


if __name__ == "__main__":
    main()
