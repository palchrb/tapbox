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

# 5. go-librespot unreachable -> falls through gracefully
def _boom():
    raise OSError("down")

daemon.spotify_playing = _boom
MPV["pause"] = False
MPV["path"] = "/cache/show/e1.mp3"
orch._mpv_alive = lambda: True
assert daemon._streaming_now() is False
print("5. unreachable go-librespot: no crash, not streaming OK")


# --- the governor loop ------------------------------------------------------
class StopLoop(Exception):
    pass


def run_governor(get_seq, streaming_seq):
    """Run the governor: get_seq scripts the baseline get_power_save
    reads (last value repeats), streaming_seq the _streaming_now ticks.
    Returns the iw set-calls made."""
    calls = []
    gets = list(get_seq)
    seq = list(streaming_seq)
    os.environ["TAPBOX_WIFI_PS_BASELINE_TRIES"] = str(max(2, len(gets)))

    def fake_run(cmd, **k):
        if "get" in cmd:
            class R:
                stdout = gets.pop(0) if len(gets) > 1 else gets[0]
            return R()
        calls.append(tuple(cmd))

        class R2:
            stdout = ""
        return R2()

    def fake_sleep(_s):
        if _s == 10:  # the baseline poll's pacing — free in tests
            return
        if not seq:
            raise StopLoop

    daemon.subprocess.run = fake_run
    daemon._streaming_now = lambda: seq.pop(0)
    real_sleep = _time.sleep
    _time.sleep = fake_sleep
    try:
        daemon._wifi_ps_governor()
    except StopLoop:
        pass
    finally:
        _time.sleep = real_sleep
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

print("WIFI PS GOVERNOR OK — power save off only while streaming, "
      "battery naps when idle, operator choice respected.")
