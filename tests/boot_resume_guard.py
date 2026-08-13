#!/usr/bin/env python3
"""Gate the boot-resume rework: (a) the popup path — after a silent
grace the box ASKS on the screen (X: connect / A: box speaker) instead
of dying silently at 90s (field 2026-07-18 18:01: box came up mute);
(b) the triple-start race — boot resume is the LAST of three possible
starters (A-press replay, transport-up blip resume), so play(boot=True)
must stand down when anyone already spawned or Spotify already plays."""
import json
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
# seconds, not minutes: grace 0.2s, tick 0.05s, popup window 2s
os.environ["VIBB_BOOT_GRACE"] = "0.2"
os.environ["VIBB_BOOT_TICK"] = "0.05"
os.environ["VIBB_BT_WAIT_S"] = "2"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
TARGET = "https://feeds.example.com/show"


# --- play(boot=True): the race guard ---------------------------------------

SPAWNED, STOPPED = [], []
daemon.Orchestrator._spawn = (
    lambda self, target, *a, **kw: SPAWNED.append(target))
daemon.Orchestrator._stop_child = lambda self: STOPPED.append(1)
daemon._kick_bt_connect = lambda: None

# 1. someone (A-press / blip resume) already spawned -> stand down; a
# boot play must NOT stop-and-respawn a child whose session is warming up
orch.child_started = time.monotonic()
r = daemon.ORCH.play(TARGET, boot=True)
assert r == {"status": "already-started"}, r
assert not SPAWNED and not STOPPED, (SPAWNED, STOPPED)
print("1. boot play stands down after another starter spawned OK")

# 2. nothing spawned here, but Spotify already streams (Connect from a
# phone, or the blip path resumed the session directly) -> stand down
orch.child_started = 0.0
daemon.go_status = lambda **k: {"track": {"uri": "spotify:track:x"},
                                "paused": False, "stopped": False}
daemon.spotify_playing = daemon._spotify.playing  # the real predicate
r = daemon.ORCH.play(TARGET, boot=True)
assert r == {"status": "already-started"}, r
assert not SPAWNED, SPAWNED
print("2. boot play stands down when spotify already plays OK")

# 3. API busy/down (OSError) must not block a legitimate boot resume —
# the child_started guard suffices; idle box -> the play proceeds
def _boom(**_k):
    raise OSError("api busy")

daemon.go_status = _boom
r = daemon.ORCH.play(TARGET, boot=True)
assert SPAWNED == [TARGET], SPAWNED
print("3. idle box: boot play proceeds (api errors fail open) OK")

# 4. a NON-boot play never takes the guard (pressing A must always work)
orch.child_started = time.monotonic()
SPAWNED.clear()
daemon.ORCH.play(TARGET)
assert SPAWNED == [TARGET], "a user play must never stand down"
print("4. user play ignores the boot guard OK")


# --- _boot_resume: grace -> popup -> armed resume ---------------------------

def write_last():
    with open(daemon.LAST_FILE, "w") as f:
        json.dump({"was_playing": True, "target": TARGET, "resume": True}, f)


def set_output(device):
    with open(daemon.OUT_FILE, "w") as f:
        json.dump({"output": device, "pcm": f"vibb_{device}"}, f)


daemon.load_settings = lambda: {"resume_window_h": -1}   # always


class _FakeSock:
    def create_connection(self, *a, **k):
        class C:
            def close(self):
                pass
        return C()

daemon.socket = _FakeSock()  # (legacy stub — probe goes via _internet_up now)
daemon._internet_up = lambda: True  # the mpv path's wifi wait — instant

READY = [False]
daemon._audio_ready = lambda: READY[0]
KICKED = []
daemon._kick_bt_connect = lambda: KICKED.append(1)
PLAYED = []
daemon.ORCH.play = lambda target, **kw: PLAYED.append((target, kw))

# 5. bt output, speaker away past the grace -> the popup kick fires
# EXACTLY once; when the transport shows up the resume still runs with
# boot=True and the wait-state is claimed (no second starter later)
set_output("bt")
write_last()
READY[0] = False
daemon._BT_WAIT.update(since=123.0, lost=456.0, ready_until=789.0)
t = threading.Thread(target=daemon._boot_resume)
t.start()
deadline = time.monotonic() + 1.5
while time.monotonic() < deadline and not KICKED:
    time.sleep(0.02)
assert KICKED == [1], f"popup must be asked after the grace: {KICKED}"
assert not PLAYED, "must not play before the audio path is up"
READY[0] = True
t.join(timeout=3)
assert not t.is_alive(), "boot resume never finished"
assert KICKED == [1], f"the popup is asked ONCE, not per tick: {KICKED}"
assert PLAYED == [(TARGET, {"reverse": False, "resume": True,
                            "boot": True})], PLAYED
assert daemon._BT_WAIT["since"] == 0.0 and daemon._BT_WAIT["lost"] == 0.0 \
    and daemon._BT_WAIT["ready_until"] == 0.0, \
    f"the wait state must be claimed before playing: {daemon._BT_WAIT}"
print("5. grace -> one popup kick -> armed resume plays with boot=True OK")

# 6. speaker away FAST (within the grace): no popup, silence stays calm
set_output("bt")
write_last()
KICKED.clear()
PLAYED.clear()
READY[0] = True  # ready before the grace elapses
daemon._boot_resume()
assert not KICKED, "no popup when the speaker was back within the grace"
assert len(PLAYED) == 1, PLAYED
print("6. speaker back within the grace: no popup, straight resume OK")

# 7. built-in output: never a popup, resume as soon as audio is up
set_output("local")
write_last()
KICKED.clear()
PLAYED.clear()
READY[0] = False
t = threading.Thread(target=daemon._boot_resume)
t.start()
time.sleep(0.5)  # well past the grace
assert not KICKED, "the popup is a bt affair — never on the built-in output"
READY[0] = True
t.join(timeout=3)
assert len(PLAYED) == 1, PLAYED
print("7. built-in output: no popup, resume when audio is up OK")

# 8. audio never comes up: give up quietly after the window — one
# attempt per shutdown, so a SECOND run must be a no-op
set_output("bt")
write_last()
KICKED.clear()
PLAYED.clear()
READY[0] = False
daemon._boot_resume()  # runs the full (shrunken) window, then gives up
assert not PLAYED, "must not play without an audio path"
KICKED.clear()
daemon._boot_resume()  # was_playing already consumed
assert not KICKED and not PLAYED, "one resume attempt per shutdown"
print("8. audio never up: gives up; one attempt per shutdown OK")

# 9. the resume WINDOW (2026-08-13): a session older than the setting
#    consumes its arm-bit and plays nothing — the box wakes up in the
#    menu instead of three days inside an album. The clock must be
#    trustworthy to conclude that, so mark it as the RTC load does.
from vibb import paths as _paths  # noqa: E402

daemon.load_settings = lambda: {"resume_window_h": 6}
_paths.note_clock_ok()
READY[0] = True
KICKED.clear()
PLAYED.clear()
write_last()
daemon.ORCH.boot_stopped_at = time.time() - 99 * 3600   # off for days
daemon._SESSION.update(verdict=None, live=True)
daemon._boot_resume()
assert not PLAYED, "an expired session must not start audio"
with open(daemon.LAST_FILE) as f:
    assert not json.load(f)["was_playing"], \
        "an expired boot must still consume the flag (no live leftover)"

# ...while a session from an hour ago continues exactly as before
KICKED.clear()
PLAYED.clear()
write_last()
daemon.ORCH.boot_stopped_at = time.time() - 3600
daemon._SESSION.update(verdict=None, live=True)
daemon.ORCH.child_started = 0.0
daemon._boot_resume()
assert PLAYED, "a session inside the window must still resume"
print("9. resume window: old session starts fresh, recent one continues OK")

print("BOOT RESUME GUARD OK — grace then ask, one starter wins the "
      "race, and no silent deaths.")
