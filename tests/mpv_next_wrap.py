#!/usr/bin/env python3
"""Gate mpv's NEXT semantics — the mirror of the prev wrap. Resume
rotates the queue so the resumed episode sits in slot 0; the LAST slot
therefore holds a real episode, but mpv's playlist-next is a no-op
there, so 'next' got stuck and fell through to 'nothing to control' —
the kid could reach that slot-0 episode only by pressing prev or
letting the last one play out (field 2026-07-20, the 3-episode NRK
series 'ninas-hemmelige-reise': next stuck on ep 2). Contract: next at
the last slot WRAPS to slot 0, and a live mpv session always owns the
transport — a non-success IPC never falls through to a spotify replay
or the misleading 'nothing to control'."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
orch._mpv_alive = lambda: True
orch.source = "mpv"
orch.target = "https://radio.nrk.no/serie/ninas-hemmelige-reise"
daemon._kick_bt_connect = lambda: None

MPV = {"playback-time": 5.0, "playlist-pos": 2, "playlist-count": 3}
SENT = []
IPC_RESULT = {"error": "success"}
daemon.mpv_get = lambda prop: MPV.get(prop)
daemon.mpv_ipc = lambda cmd: (SENT.append(list(cmd)), IPC_RESULT)[1]

# 1. next at the LAST slot wraps to slot 0 (the stuck-on-ep-2 bug)
SENT.clear()
r = orch.command("next")
assert r["routed"] == "mpv", r
assert SENT == [["set_property", "playlist-pos", 0]], SENT
print("1. next at the last slot wraps to the first episode OK")

# 2. next mid-playlist is a plain playlist-next
SENT.clear()
MPV["playlist-pos"] = 0
r = orch.command("next")
assert r["routed"] == "mpv"
assert SENT == [["playlist-next"]], SENT
print("2. next mid-playlist stays playlist-next OK")

# 3. a single-item queue can't wrap — playlist-next (harmless no-op)
SENT.clear()
MPV["playlist-pos"], MPV["playlist-count"] = 0, 1
r = orch.command("next")
assert SENT == [["playlist-next"]], SENT
print("3. single-item queue: no bogus wrap OK")

# 4. AUTHORITATIVE: a non-success IPC (end of queue / transient refusal)
# is still owned by the live mpv session — it must NOT fall through to
# 'nothing to control' (which risked replaying the wrong source)
SENT.clear()
MPV["playlist-pos"], MPV["playlist-count"] = 1, 3
IPC_RESULT = {"error": "property unavailable"}
daemon.go_status = lambda **k: {}  # rules 2-4 would see nothing
r = orch.command("next")
assert r["routed"] == "mpv", f"a live mpv must own the control: {r}"
print("4. a non-success mpv IPC never falls through to 'nothing' OK")

# 5. playpause still cycles pause on the live session
SENT.clear()
IPC_RESULT = {"error": "success"}
r = orch.command("playpause")
assert r["routed"] == "mpv" and SENT == [["cycle", "pause"]], SENT
print("5. playpause cycles pause on the mpv session OK")

print("MPV NEXT WRAP OK — next reaches every episode by wrapping the "
      "rotated queue, and a live mpv session always owns the transport.")
