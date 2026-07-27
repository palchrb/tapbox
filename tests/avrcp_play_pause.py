#!/usr/bin/env python3
"""Gate AVRCP play/pause semantics (field 2026-07-27, Skoda head unit).

AVRCP sends DISTINCT play and pause commands. Every one of them used to
map onto "playpause", a TOGGLE — so a car saying "play" to a box that
was already playing PAUSED it, the car sent play again, and the whole
drive was one long stutter. The log showed the fight directly: repeated
`playpause -> mpv` seconds apart with nobody touching a button.

The rule under test: an explicit command must be IDEMPOTENT. Only
KEY_PLAYPAUSE toggles. That also makes a peer that re-announces its
state on connect harmless, instead of a source of stutter."""
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_LIBRARY"] = os.path.join(TMP, "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import buttons  # noqa: E402
import daemon  # noqa: E402

# 0. the method must NOT be called resume(): self.resume is already the
#    library entry's resume-position flag, so a method of that name is
#    shadowed by the instance attribute and calling it raises TypeError
assert isinstance(getattr(daemon.ORCH, "resume", None), bool), \
    "ORCH.resume is the entry flag — the unpause method must not shadow it"
assert callable(daemon.ORCH.unpause)
print("0. unpause() doesn't collide with the resume flag OK")

# 1. THE BUG: the three explicit AVRCP keys must not be toggles
assert buttons.ACTIONS[200] == "resume", "KEY_PLAYCD must mean play, not toggle"
assert buttons.ACTIONS[201] == "pause", "KEY_PAUSECD must mean pause"
assert buttons.ACTIONS[166] == "pause", "KEY_STOPCD must not toggle"
assert buttons.ACTIONS[164] == "playpause", "KEY_PLAYPAUSE IS the toggle key"
print("1. explicit AVRCP keys map to idempotent commands OK")

# 2. ...and they reach the daemon as themselves
sent = []
buttons.boxapi = types.SimpleNamespace(
    post=lambda path, body=None, **k: (sent.append((path, body)),
                                       {"routed": "mpv"})[1])
for code, expect in ((200, "/resume"), (201, "/pause"), (166, "/pause"),
                     (164, "/playpause")):
    sent.clear()
    buttons.handle(buttons.ACTIONS[code])
    assert sent and sent[0][0] == expect, (code, sent)
print("2. each key posts its own endpoint (no collapsing to playpause) OK")

# 3. THE IDEMPOTENCE THAT FIXES THE STUTTER: resume on an already-playing
#    box must NOT pause it. This is the exact car-vs-box fight.
orch = daemon.ORCH
state = {"pause": False}


def fake_ipc(cmd):
    if cmd[:2] == ["set_property", "pause"]:
        state["pause"] = cmd[2]          # explicit: resume/pause
    elif cmd == ["cycle", "pause"]:
        state["pause"] = not state["pause"]  # the real toggle
    return {"error": "success"}


daemon.mpv_ipc = fake_ipc
orch._mpv_alive = lambda: True

orch.unpause()
assert state["pause"] is False, "resume while playing must stay playing"
orch.unpause()
orch.unpause()
assert state["pause"] is False, "repeated resume must never toggle"
print("3. repeated resume on a playing box never pauses it OK")

# 3b. and the mirror: repeated pause never starts it again
orch.pause()
assert state["pause"] is True
orch.pause()
assert state["pause"] is True, "repeated pause must never toggle back"
print("3b. repeated pause never resumes OK")

# 3c. resume actually resumes a paused box (it must still DO something)
orch.unpause()
assert state["pause"] is False
print("3c. resume unpauses a paused box OK")

# 4. the old toggle still toggles — a real play/pause button must work
daemon.spotify_playing = lambda *a, **k: False
daemon._kick_bt_connect = lambda: None
orch.source = "mpv"   # command() routes the toggle by source
before = state["pause"]
orch.command("playpause")
assert state["pause"] != before, "KEY_PLAYPAUSE must still toggle"
print("4. the genuine toggle key still toggles OK")

# 5. spotify: resume must not pause a live session either
orch._mpv_alive = lambda: False
orch.source = "spotify"
calls = []
daemon.go = lambda path, **k: calls.append(path)
daemon.go_status = lambda **k: {"track": {"uri": "x"}, "paused": False,
                                "stopped": False}
orch.unpause()
assert "/player/pause" not in calls, f"resume must never pause spotify: {calls}"
assert calls == [], "an already-playing spotify session needs no call"
daemon.go_status = lambda **k: {"track": {"uri": "x"}, "paused": True,
                                "stopped": False}
orch.unpause()
assert calls == ["/player/resume"], calls
print("5. spotify: resume is a no-op when playing, resumes when paused OK")

# 6. the transport debounce covers the new commands too — a peer that
#    machine-guns play/pause churns the A2DP transport, which is a
#    firmware-crash trigger on this box
src = open(os.path.join(REPO, "pi", "buttons.py")).read()
assert 'action in ("playpause", "resume", "pause")' in src, \
    "the sub-350ms debounce must cover resume/pause, not just playpause"
print("6. the repeat debounce covers every transport command OK")

print("\nall avrcp_play_pause checks passed")
