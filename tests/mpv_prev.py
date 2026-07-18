#!/usr/bin/env python3
"""Gate mpv's prev semantics. Resume ROTATES the queue so the bookmarked
episode sits in playlist slot 0 — so mpv's playlist-prev is a no-op
there, and the second prev press fell through to 'nothing to control'
(field 2026-07-18: 'prev just restarts the same track'). Contract:
prev >5s into a track restarts it; prev near the start goes to the
PREVIOUS episode, wrapping from slot 0 to the end of the playlist
(which, after rotation, IS the episode before the bookmark)."""
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
orch.target = "https://radio.nrk.no/podkast/show"
daemon._kick_bt_connect = lambda: None

MPV = {"playback-time": 120.0, "playlist-pos": 0, "playlist-count": 36}
SENT = []
daemon.mpv_get = lambda prop: MPV.get(prop)
daemon.mpv_ipc = lambda cmd: (SENT.append(list(cmd)), {"error": "success"})[1]

# 1. deep into the episode: prev restarts it (standard player semantics)
SENT.clear()
r = orch.command("prev")
assert r["routed"] == "mpv"
assert SENT == [["seek", 0, "absolute"]], SENT
print("1. prev >5s in restarts the episode OK")

# 2. near the start, in slot 0 (the rotated bookmark): prev WRAPS to the
# end of the playlist — the episode before the bookmark — instead of
# playlist-prev no-op'ing into 'nothing to control'
SENT.clear()
MPV["playback-time"] = 2.0
r = orch.command("prev")
assert r["routed"] == "mpv", r
assert SENT == [["set_property", "playlist-pos", 35]], SENT
print("2. prev at the rotated slot 0 wraps to the previous episode OK")

# 3. mid-playlist: plain playlist-prev as before
SENT.clear()
MPV["playlist-pos"] = 7
r = orch.command("prev")
assert SENT == [["playlist-prev"]], SENT
print("3. prev mid-playlist stays playlist-prev OK")

# 4. a single-item queue can't wrap — playlist-prev (harmless no-op)
SENT.clear()
MPV["playlist-pos"] = 0
MPV["playlist-count"] = 1
r = orch.command("prev")
assert SENT == [["playlist-prev"]], SENT
print("4. single-item queue: no bogus wrap OK")

# 5. next is untouched
SENT.clear()
MPV["playlist-count"] = 36
r = orch.command("next")
assert SENT == [["playlist-next"]], SENT
print("5. next unchanged OK")

print("MPV PREV OK — restart on the first press, previous episode on the "
      "second, wrapping across the rotated queue.")
