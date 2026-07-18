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


def run_ticks(n, internet, go_st):
    """Drive n supervisor iterations with everything stubbed. go_st is
    what go_status returns (a callable to raise instead)."""
    CALLS.clear()
    left = [n]

    def fake_sleep(_s):
        if left[0] <= 0:
            raise StopLoop
        left[0] -= 1

    real_sleep = _time.sleep
    _time.sleep = fake_sleep
    daemon._internet_up = lambda: internet
    daemon.go_status = go_st if callable(go_st) else (lambda **k: go_st)
    daemon._spotify.lock = lambda: False
    daemon.subprocess.run = lambda cmd, **k: CALLS.append(tuple(cmd))
    try:
        daemon._spotify_supervisor()
    except StopLoop:
        pass
    finally:
        _time.sleep = real_sleep


CALLS = []
PLAYING = {"track": {"uri": "spotify:track:x"}, "paused": False}
PAUSED = {"track": {"uri": "spotify:track:x"}, "paused": True}

# 1. offline probe misses while Spotify PLAYS -> never parked, no banner
daemon._SPOT_OFFLINE[0] = False
run_ticks(5, internet=False, go_st=PLAYING)
assert CALLS == [], f"parked mid-play: {CALLS}"
assert daemon._SPOT_OFFLINE[0] is False, "false offline banner mid-play"
print("1. probe misses while playing: never parked, no banner OK")

# 1b. a PAUSED session is protected too — parking it destroys the kid's
# pause: the session dies and the next button hits 'session is empty ->
# replaying last', restarting the music (field 2026-07-18 15:13-15:15,
# pausing fought the parker for two minutes during the cache sweep)
daemon._SPOT_OFFLINE[0] = False
run_ticks(5, internet=False, go_st=PAUSED)
assert CALLS == [], f"parked a paused session: {CALLS}"
assert daemon._SPOT_OFFLINE[0] is False
print("1b. a paused session is never parked (pause survives) OK")

# 2. offline with NO session loaded -> parked after the 2-miss hysteresis
daemon._SPOT_OFFLINE[0] = False
run_ticks(5, internet=False, go_st={})
assert ("systemctl", "stop", "go-librespot") in CALLS, CALLS
assert daemon._SPOT_OFFLINE[0] is True
print("2. offline and idle (no session): parked after two misses OK")

# 3. go-librespot unreachable (OSError) counts as no-session -> parks
daemon._SPOT_OFFLINE[0] = False

def _boom(**_k):
    raise OSError("connection refused")

run_ticks(5, internet=False, go_st=_boom)
assert ("systemctl", "stop", "go-librespot") in CALLS, CALLS
print("3. go-librespot unreachable: parks as before OK")

# 4. internet back after a park -> started again, banner cleared
daemon._SPOT_OFFLINE[0] = True
run_ticks(2, internet=True, go_st={})
assert daemon._SPOT_OFFLINE[0] is False
print("4. internet back: offline banner clears OK")

print("SPOT SUPERVISOR OK — a loaded session (playing OR paused) is never "
      "parked; idle offline still parks after the hysteresis.")
