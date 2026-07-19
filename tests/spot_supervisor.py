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
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()  # no stale radio markers
# 0 by default: the boot-grace scenarios opt in — and a CI container's
# real uptime may be under the 180s default, which would mask the
# parking scenarios entirely
os.environ["TAPBOX_SPOT_PARK_GRACE"] = "0"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402


class StopLoop(Exception):
    pass


def run_ticks(n, internet, go_st):
    """Drive n supervisor iterations with everything stubbed. go_st is
    what go_status returns (a callable to raise instead). Ticks go
    through daemon._tick — patching the global time.sleep also hit the
    daemon's OTHER live threads (arbiter/watchdog), which stole
    scripted ticks and could catch StopLoop themselves (QA review Q2)."""
    CALLS.clear()
    left = [n]

    def fake_tick(_s):
        if left[0] <= 0:
            raise StopLoop
        left[0] -= 1

    real_tick = daemon._tick
    daemon._tick = fake_tick
    daemon._internet_up = lambda: internet
    daemon.go_status = go_st if callable(go_st) else (lambda **k: go_st)
    daemon._spotify.lock = lambda: False
    daemon.subprocess.run = lambda cmd, **k: CALLS.append(tuple(cmd))
    try:
        daemon._spotify_supervisor()
    except StopLoop:
        pass
    finally:
        daemon._tick = real_tick


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
# (unit active -> the stop is actually issued; see 3b for the dead case)
daemon._SPOT_OFFLINE[0] = False
daemon._go_unit_active = lambda: True
run_ticks(5, internet=False, go_st={})
assert ("systemctl", "stop", "go-librespot") in CALLS, CALLS
assert daemon._SPOT_OFFLINE[0] is True
print("2. offline and idle (no session): parked after two misses OK")

# 3. unreachable API but the unit is ACTIVE = busy, not dead — a rapid
# next/prev burst makes the HTTP api time out while music plays; parking
# then kills it (field 2026-07-18 15:44:38). Never park a running unit.
daemon._SPOT_OFFLINE[0] = False

def _boom(**_k):
    raise OSError("connection refused")

daemon._go_unit_active = lambda: True
run_ticks(5, internet=False, go_st=_boom)
assert CALLS == [], f"parked a busy-but-running go-librespot: {CALLS}"
assert daemon._SPOT_OFFLINE[0] is False
print("3. unreachable + unit active = busy: never parked OK")

# 3b. unreachable AND the unit is down -> genuinely dead: marked offline,
# but NO systemctl stop is forked for an already-dead unit (review P1 —
# the old code forked one every 20s tick forever on an offline cabin box)
daemon._SPOT_OFFLINE[0] = False
daemon._go_unit_active = lambda: False
run_ticks(5, internet=False, go_st=_boom)
assert ("systemctl", "stop", "go-librespot") not in CALLS, CALLS
assert daemon._SPOT_OFFLINE[0] is True, "dead unit must still read offline"
print("3b. unreachable + unit down: offline flagged, no pointless stop OK")

# 4. internet back after a park -> started again, banner cleared
daemon._SPOT_OFFLINE[0] = True
run_ticks(2, internet=True, go_st={})
assert daemon._SPOT_OFFLINE[0] is False
print("4. internet back: offline banner clears OK")

# 5. boot grace: the first minutes of uptime are a storm of
# self-inflicted radio events (BT boot pages deauthed wifi mid-DHCP and
# parked go-librespot 70s after boot on a FALSE 'no internet' — field
# 2026-07-18 20:17:11). Within the grace nothing parks and no banner
# shows, however many probes fail.
os.environ["TAPBOX_SPOT_PARK_GRACE"] = "999999999"
daemon._SPOT_OFFLINE[0] = False
daemon._go_unit_active = lambda: True
run_ticks(6, internet=False, go_st={})
assert CALLS == [], f"parked within the boot grace: {CALLS}"
assert daemon._SPOT_OFFLINE[0] is False, "false offline banner in the grace"
os.environ["TAPBOX_SPOT_PARK_GRACE"] = "0"
print("5. boot grace: no parking, no banner in the first minutes OK")

# 6. a fresh PAGING marker (btwatchd mid-connect) skips the probe tick
# entirely — a page owns the radio, so the probe result is noise either
# way (a page-deauthed wifi read as 'offline' in the field)
daemon._SPOT_OFFLINE[0] = False
daemon._radio.touch_paging()
run_ticks(6, internet=False, go_st={})
assert CALLS == [] and daemon._SPOT_OFFLINE[0] is False, CALLS
daemon._radio.clear_paging()
run_ticks(5, internet=False, go_st={})
assert ("systemctl", "stop", "go-librespot") in CALLS, \
    "parking must resume once the page is over"
print("6. probes hold during a BT page, resume after OK")

print("SPOT SUPERVISOR OK — a loaded session (playing OR paused) is never "
      "parked; idle offline still parks after the hysteresis.")
