#!/usr/bin/env python3
"""Gate the context-SWITCH bookmark flush.

Switching from spotify url A to a DIFFERENT url B does not tear down the
bookmarker thread (unlike player.py, which flushes bm_pending when mpv exits)
— it just moves to B and drops A's throttled bm_pending, so the last <=30s of
A (incl. a seek just made) would be lost. play() must flush the outgoing
position first, and must NOT flush on a same-url replay (nothing to hand off).
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
orch.child = None

# neutralise the heavy bits of play() so only the flush wiring is exercised
orch._ensure_spotify_backend = lambda: True
orch._spawn = lambda *a, **k: None
orch._stop_child = lambda: None
daemon._kick_bt_connect = lambda *a, **k: None
daemon.go_status = lambda **k: {}

flushes = []
daemon._flush_spotify_bookmark = lambda: flushes.append(1)

A = "https://open.spotify.com/playlist/AAAAAAAAAAAAAAAAAAAAAA"
B = "https://open.spotify.com/playlist/BBBBBBBBBBBBBBBBBBBBBB"

# 1. switching to a DIFFERENT url flushes the outgoing position first
orch.source, orch.target = "spotify", A
orch.play(B)
assert flushes == [1], "switching url must flush the outgoing bookmark"
assert orch.target == B
print("1. switching to a different url flushes the outgoing bookmark OK")

# 2. replaying the SAME url does not (guard target != self.target)
flushes.clear()
orch.play(B)  # target == self.target now
assert flushes == [], "same-url replay must not trigger the switch flush"
print("2. same-url replay does not trigger the switch flush OK")

print("\nall spot_switch_bookmark_flush checks passed")
