#!/usr/bin/env python3
"""Gate the idle auto-shutdown: playback resets the countdown, button
presses reset it too (a kid browsing without playing must never have
the box die in their hands), a paused/stopped box counts down and
powers off at the limit, 'never' (0) both disables AND stops the count
from accumulating behind the parent's back — flipping auto-off back on
after hours of tomgang must start a FRESH countdown, not power off
within the minute — and a future-mtime activity marker (clock jump) is
no signal, exactly like the radio markers."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = RUN
os.environ["TAPBOX_SETTINGS"] = os.path.join(RUN, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import json  # noqa: E402

import idle  # noqa: E402
from tapbox import paths  # noqa: E402

CALLS = []
idle.subprocess.run = lambda argv, **kw: CALLS.append(argv[0])
PLAYING = [True]
idle.daemon_playing = lambda: PLAYING[0]


def set_limit(minutes):
    with open(os.environ["TAPBOX_SETTINGS"], "w") as f:
        json.dump({"idle_shutdown_min": minutes}, f)


set_limit(2)  # 2 min limit -> third idle cycle (120s) powers off

# 1. playback keeps resetting the countdown
assert idle._cycle(999) == 0
print("1. active playback resets the countdown OK")

# 2. paused/stopped: counts down and powers off at the limit
PLAYING[0] = False
n = idle._cycle(0)
assert n == idle.CHECK_S and CALLS == []
assert idle._cycle(n) is None  # 120s idle == the 2 min limit
assert CALLS == ["logger", "poweroff"], CALLS
print("2. idle box powers off exactly at the limit OK")

# 3. a fresh button press counts as activity — browsing hands never
# have the box shut down under them
CALLS.clear()
paths._ACT_TOUCHED[0] = 0.0
paths.touch_activity()
assert idle._cycle(999) == 0 and CALLS == []
print("3. fresh button press resets the countdown OK")

# 4. a stale press does not: age it beyond the freshness window
old = time.time() - idle.ACTIVITY_FRESH_S - 5
os.utime(paths.ACTIVITY_FILE, (old, old))
assert idle._cycle(0) == idle.CHECK_S
print("4. stale button press no longer counts OK")

# 5. a future-mtime marker (clock jumped backwards) is no signal
future = time.time() + 3600
os.utime(paths.ACTIVITY_FILE, (future, future))
assert idle._cycle(0) == idle.CHECK_S
print("5. future-mtime marker (clock jump) is ignored OK")

# 6. 'never' (0): no poweroff AND no accumulation — hours of tomgang
# then flipping auto-off back on starts fresh, not instant shutdown
os.remove(paths.ACTIVITY_FILE)
set_limit(0)
assert idle._cycle(999999) == 0 and CALLS == []
set_limit(2)
assert idle._cycle(0) == idle.CHECK_S and CALLS == []
print("6. 'never' disables and drains the counter — re-enable starts fresh OK")

# 7. daemon down -> the direct source probes decide
set_limit(30)
idle.daemon_playing = lambda: None
idle.spotify.playing = lambda: True
idle.mpv.playing = lambda: False
assert idle._cycle(60) == 0
idle.spotify.playing = lambda: False
assert idle._cycle(60) == 2 * idle.CHECK_S
print("7. daemon down falls back to direct source probes OK")

# 8. the marker helpers themselves: throttled writes, epoch mtime
paths._ACT_TOUCHED[0] = 0.0
paths.touch_activity()
first = paths.last_activity()
assert first > 0
time.sleep(0.05)
paths.touch_activity()  # throttled — must NOT rewrite within 10s
assert paths.last_activity() == first
print("8. activity marker writes once per burst (throttled) OK")

print("IDLE SHUTDOWN OK — playback or hands on the box keep it alive, "
      "'never' never counts, and it dies exactly at the parent's limit.")
