#!/usr/bin/env python3
"""Gate the offline supervisor's parking rules. Field 2026-07-18 15:09:
the cache sweep's downloads + the Spotify stream + A2DP saturated the
shared 2.4GHz radio, the internet probe missed twice, and the supervisor
parked go-librespot MID-SONG — ~13s of silence and a track restart over
a false 'offline'. Audio streaming right now is proof the session lives:
never park while Spotify is playing; a truly dead net stops playback by
itself and THEN parking is fine."""
import os
import sys
import tempfile
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402


class StopLoop(Exception):
    pass


def run_ticks(n, internet, playing):
    """Drive n supervisor iterations with everything stubbed."""
    CALLS.clear()
    left = [n]

    def fake_sleep(_s):
        if left[0] <= 0:
            raise StopLoop
        left[0] -= 1

    real_sleep = _time.sleep
    _time.sleep = fake_sleep
    daemon._internet_up = lambda: internet
    daemon.spotify_playing = lambda: playing
    daemon._spotify.lock = lambda: False
    daemon.subprocess.run = lambda cmd, **k: CALLS.append(tuple(cmd))
    try:
        daemon._spotify_supervisor()
    except StopLoop:
        pass
    finally:
        _time.sleep = real_sleep


CALLS = []

# 1. offline probe misses while Spotify PLAYS -> never parked, no banner
daemon._SPOT_OFFLINE[0] = False
run_ticks(5, internet=False, playing=True)
assert CALLS == [], f"parked mid-play: {CALLS}"
assert daemon._SPOT_OFFLINE[0] is False, "false offline banner mid-play"
print("1. probe misses while playing: never parked, no banner OK")

# 2. offline and NOT playing -> parked after the 2-miss hysteresis
daemon._SPOT_OFFLINE[0] = False
run_ticks(5, internet=False, playing=False)
assert ("systemctl", "stop", "go-librespot") in CALLS, CALLS
assert daemon._SPOT_OFFLINE[0] is True
print("2. offline and idle: parked after two misses OK")

# 3. go-librespot unreachable (OSError) counts as not-playing -> parks
daemon._SPOT_OFFLINE[0] = False

def _boom():
    raise OSError("connection refused")

CALLS.clear()
left = [5]

def fake_sleep(_s):
    if left[0] <= 0:
        raise StopLoop
    left[0] -= 1

real_sleep = _time.sleep
_time.sleep = fake_sleep
daemon._internet_up = lambda: False
daemon.spotify_playing = _boom
daemon.subprocess.run = lambda cmd, **k: CALLS.append(tuple(cmd))
try:
    daemon._spotify_supervisor()
except StopLoop:
    pass
finally:
    _time.sleep = real_sleep
assert ("systemctl", "stop", "go-librespot") in CALLS, CALLS
print("3. go-librespot unreachable: parks as before OK")

# 4. internet back after a park -> started again, banner cleared
daemon._SPOT_OFFLINE[0] = True
run_ticks(2, internet=True, playing=False)
# (parked-state is loop-local; what must hold is the banner clearing)
assert daemon._SPOT_OFFLINE[0] is False
print("4. internet back: offline banner clears OK")

print("SPOT SUPERVISOR OK — playing audio is never parked; idle offline "
      "still parks after the hysteresis.")
