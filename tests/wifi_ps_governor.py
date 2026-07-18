#!/usr/bin/env python3
"""Gate the wifi power-save governor: PS off ONLY while audio streams
over the network (Spotify / remote mpv URL), back on when idle or when
playing cached files (there, less wifi airtime is BETTER for A2DP on the
shared antenna). Under BT coexistence, PS latency spikes starved
go-librespot's control plane (field 2026-07-18 15:30: put-state deadline
exceeded, /next timeouts). Respects an operator's PS-off boot state."""
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
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH

# --- _streaming_now: the decision ------------------------------------------
MPV = {"pause": False, "path": "/cache/show/e1.mp3"}
daemon.mpv_get = lambda p: MPV.get(p)
daemon.spotify_playing = lambda: False
orch._mpv_alive = lambda: True

# 1. cached local file playing -> NOT streaming (PS stays on: less wifi
# airtime helps A2DP)
assert daemon._streaming_now() is False
print("1. cached playback: not streaming (PS stays on) OK")

# 2. a remote mpv URL playing -> streaming
MPV["path"] = "https://podkast.nrk.no/x/e1.mp3"
assert daemon._streaming_now() is True
print("2. remote mpv URL: streaming OK")

# 3. paused remote -> not streaming
MPV["pause"] = True
assert daemon._streaming_now() is False
print("3. paused: not streaming OK")

# 4. spotify playing -> streaming, regardless of mpv
daemon.spotify_playing = lambda: True
assert daemon._streaming_now() is True
print("4. spotify playing: streaming OK")

# 5. go-librespot unreachable + unit DOWN -> genuinely not streaming
def _boom():
    raise OSError("down")

daemon.spotify_playing = _boom
daemon._go_unit_active = lambda: False
MPV["pause"] = False
MPV["path"] = "/cache/show/e1.mp3"
orch._mpv_alive = lambda: True
assert daemon._streaming_now() is False
print("5. unreachable + unit down: not streaming OK")

# 5b. unreachable + unit RUNNING = mid-track-load (the api blocks while
# loading — exactly when the radio is busiest): UNKNOWN, never 'idle'.
# The governor once flipped power save ON in that blind spot and
# stretched a CDN load to ~19s (field 2026-07-18 16:14:44).
daemon._go_unit_active = lambda: True
assert daemon._streaming_now() is None
print("5b. unreachable + unit running: unknown (hold PS state) OK")

# 5c. the sweep's audible-gate has the same blind spot: api blocked +
# unit running counts as BUSY (no sweep downloads mid-load)
orch._mpv_alive = lambda: False
assert daemon._audible_now() is True
daemon._go_unit_active = lambda: False
assert daemon._audible_now() is False
orch._mpv_alive = lambda: True
print("5c. audible-gate: blocked api + running unit = busy OK")

# 5d. a fresh BUSY marker = a start/skip is in flight NOW, whatever the
# api answers — during a /next the api can read idle-ish mid-load and
# the governor flipped PS ON in the middle of the CDN fetch (field
# 2026-07-18 20:26: a 23s skip)
daemon._radio.touch_busy()
assert daemon._streaming_now() is True
old = _time.time() - daemon._radio.BUSY_TTL_S - 1
os.utime(daemon._radio.BUSY_FILE, (old, old))  # marker stale again
print("5d. fresh busy marker counts as streaming OK")

# 5e. a control that timed out moments ago = a load is very likely in
# flight: unknown, never idle. An ancient stamp must NOT hold.
daemon.spotify_playing = lambda st=None: False
daemon.ORCH._spot_cmd_timeout_at = _time.monotonic()
assert daemon._streaming_now() is None
daemon.ORCH._spot_cmd_timeout_at = -1e9
MPV["pause"] = True
assert daemon._streaming_now() is False
print("5e. recent control timeout holds; ancient stamp does not OK")


# --- the governor loop ------------------------------------------------------
class StopLoop(Exception):
    pass


def run_governor(get_seq, streaming_seq, hyst="0"):
    """Run the governor: get_seq scripts the baseline get_power_save
    reads (last value repeats), streaming_seq the _streaming_now ticks.
    Returns the iw set-calls made. hyst pins TAPBOX_WIFI_PS_HYST — 0 by
    default so the pre-hysteresis scenarios keep their instant flips."""
    calls = []
    gets = list(get_seq)
    seq = list(streaming_seq)
    os.environ["TAPBOX_WIFI_PS_BASELINE_TRIES"] = str(max(2, len(gets)))
    os.environ["TAPBOX_WIFI_PS_HYST"] = hyst

    def fake_run(cmd, **k):
        if "get" in cmd:
            class R:
                stdout = gets.pop(0) if len(gets) > 1 else gets[0]
            return R()
        calls.append(tuple(cmd))

        class R2:
            stdout = ""
        return R2()

    class FakeKick:  # the tick wait: ends the loop when the script is done
        def wait(self, _s):
            if not seq:
                raise StopLoop

        def clear(self):
            pass

    def fake_sleep(_s):
        if _s == 10:  # the baseline poll's pacing — free in tests
            return

    daemon.subprocess.run = fake_run
    daemon._streaming_now = lambda: seq.pop(0)
    real_kick = daemon._PS_KICK
    daemon._PS_KICK = FakeKick()
    real_sleep = _time.sleep
    _time.sleep = fake_sleep
    try:
        daemon._wifi_ps_governor()
    except StopLoop:
        pass
    finally:
        _time.sleep = real_sleep
        daemon._PS_KICK = real_kick
    return calls


# 6. streaming starts -> one 'off'; stays streaming -> no repeat; idle
# again -> one 'on'
calls = run_governor(["Power save: on\n"], [False, True, True, False])
sets = [c[-1] for c in calls]
assert sets == ["off", "on"], sets
print("6. governor: off on stream start, on when idle, no chatter OK")

# 7. PS never seen on (perf mode / operator choice) -> untouched
calls = run_governor(["Power save: off\n"], [True, False, True])
assert calls == [], f"must not manage an operator's PS-off: {calls}"
print("7. operator PS-off is respected (governor stands down) OK")

# 8. boot race: PS reads 'off' at daemon start (NetworkManager enables
# it ~2min later, tapbox-power re-asserts it) — the baseline poll must
# WAIT until it's seen on, then manage. A one-shot read stood down
# forever and left PS ON through every stream (field 2026-07-18 15:43).
calls = run_governor(["Power save: off\n", "Power save: off\n",
                      "Power save: on\n"], [True, False])
sets = [c[-1] for c in calls]
assert sets == ["off", "on"], f"late-enabled PS must still be managed: {sets}"
print("8. baseline poll waits out the boot race, then manages OK")

# 9. UNKNOWN ticks (api mid-load) hold the current state — no blind
# flip to 'on (idle)' in the middle of a CDN download
calls = run_governor(["Power save: on\n"], [True, None, None, False])
sets = [c[-1] for c in calls]
assert sets == ["off", "on"], f"unknown must hold, not flip: {sets}"
print("9. mid-load unknown holds the PS state OK")

# 10. hysteresis: after streaming stops, PS stays OFF through short
# idle — flipping ON 10s after a pause silently killed the Spotify AP
# TCP ('did not receive last pong ack', field 2026-07-18 20:24) and a
# mid-activity flip has caused a field problem every single time
calls = run_governor(["Power save: on\n"],
                     [True, False, False, False, False], hyst="3600")
sets = [c[-1] for c in calls]
assert sets == ["off"], f"short idle must not flip PS back on: {sets}"
print("10. hysteresis: short idle keeps PS off (AP link survives) OK")

# 10b. a LONG idle (clock past the window) does flip back to 'on'.
# Clock calls in the loop: idle-start stamp, then one compare per idle
# tick — script it so the second idle tick lands past the window.
real_mono = daemon.time.monotonic
clock = iter([1000.0, 1000.0, 5000.0])
last = [1000.0]

def fake_mono():
    last[0] = next(clock, last[0])
    return last[0]

daemon.time.monotonic = fake_mono
try:
    calls = run_governor(["Power save: on\n"], [True, False, False],
                         hyst="3600")
finally:
    daemon.time.monotonic = real_mono
sets = [c[-1] for c in calls]
assert sets == ["off", "on"], f"long idle must flip PS on: {sets}"
print("10b. long idle past the window flips PS back on OK")

print("WIFI PS GOVERNOR OK — power save off only while streaming, "
      "battery naps when idle, operator choice respected.")
