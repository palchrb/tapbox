#!/usr/bin/env python3
"""Boot starts NO audio, and is remembered anyway (owner 2026-08-18).

A reboot must never start playing on its own, whatever the output — it
lands on the now-playing screen for what was in progress, PAUSED, and
one tap continues from the right second. Two starters were deleted for
that: _boot_resume, and the sonos reconcile's move-the-session-onto-the-
box branch (which was the specific complaint: a Sonos session reappearing
on the built-in speaker after a reboot).

What must still work is proved here: (a) play()'s boot guard, kept as the
contract for any future boot starter even though nothing passes boot=True
today; (b) the deletions actually happened; (c) the box still REMEMBERS —
status() serves the paused ghost card the screen lands on.

Mid-session BT reconnect still auto-resumes; that is a different path
(_bt_transport_lost -> _bt_wait_watcher -> _bt_blip_resume) and is pinned
by tests/bt_lost_pause_recover.py."""
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
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


# --- the deletions, and what replaced them ---------------------------------

DSRC = open(daemon.__file__, encoding="utf-8").read()

# 5. no boot starter is left anywhere in the daemon
assert "def _boot_resume" not in DSRC, "_boot_resume must be gone"
assert "target=_boot_resume" not in DSRC, "its boot thread must be gone too"
assert "boot=True" not in DSRC, \
    "nothing may call play(boot=True) — a reboot starts no audio"
print("5. no boot starter left in the daemon OK")

# 6. the sonos reconcile still reverts the renderer to the BOX, but no
#    longer plays there. Those four lines MUST survive: without them a
#    box that was on sonos keeps a remote renderer and the next tap goes
#    to a speaker in a room nobody asked in.
i = DSRC.index("No live remote session at boot")
blk = DSRC[i:i + 1200]
for keep in ('_renderer.write("box")', "content.PREFER_REMOTE = False",
             "_library._EXPAND_CACHE.clear()", 'ORCH.source == "sonos"'):
    assert keep in blk, f"the reconcile must still do: {keep}"
assert "resuming on" not in blk and "ORCH.play(" not in blk, \
    "a reboot must not move a sonos session onto the built-in speaker"
print("6. sonos reconcile reverts the renderer, starts nothing OK")

# 7. silent but REMEMBERED: with a bookmark on disk and nothing playing,
#    status() still presents the target as paused-at-position. This is
#    what the screen lands on (tests/ui_session_landing.py owns the
#    landing rule itself), and it is the whole reason boot can be silent
#    without the box looking like it forgot.
from vibb import library as _lib  # noqa: E402

daemon.load_settings = lambda: {"resume_window_h": -1}   # always fresh
daemon.go_status = lambda **k: {}      # test 3 left an OSError stub behind
orch.target, orch.source = TARGET, "mpv"
orch.child = None
orch.resume = True
daemon._SESSION.update(verdict=None, live=True)
with open(os.path.join(STATE, _lib.state_key(TARGET) + ".json"), "w") as f:
    json.dump({"id": "ep7", "url": "https://x/7.mp3", "pos": 812.0}, f)
with open(daemon.NOW_FILE, "w") as f:
    json.dump({"target": TARGET, "title": "Episode 7", "id": "ep7",
               "duration": 1800.0}, f)

daemon.session_verdict()   # what the daemon's own boot thread does
st = orch.status()
assert st["playing"] is False, f"boot must not be playing: {st}"
assert st["title"] == "Episode 7", f"but it must remember what: {st}"
assert st["position"] == 812.0, f"...and exactly where: {st}"
assert st["session"] == "fresh", st
print("7. boot is silent but remembered: paused ghost at the right second OK")

print("BOOT RESUME GUARD OK — a reboot lands paused on what was playing, "
      "and one tap continues it.")
