#!/usr/bin/env python3
"""Gate the mid-burst status card (field 2026-07-23 22:16, mash test):
during a v0.0.8 debounced skip the fork's /status answers with
pending_track_uri and NO track for a beat. That trackless-but-non-empty
answer skipped the hold-cache branch (which only fired on a fully empty
response) and flashed 'Nothing playing' on the screen mid-mash.

Contract: a trackless /status WITH an in-flight signal (pending skip,
busy marker, recent slow control) holds the last good card for the load
window — and still passes pending_track_uri through. A trackless answer
with NO signal keeps the old behavior (a deliberate stop shows
immediately)."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
orch._mpv_alive = lambda: False
orch.source, orch.target = "spotify", "https://open.spotify.com/playlist/x"
daemon.current_output = lambda **_k: {"output": "local"}
daemon._radio.busy = lambda: False
orch._spot_cmd_timeout_at = -1e9

LIVE = {"track": {"uri": "spotify:track:a", "name": "Blue Monday '88",
                  "artist_names": ["New Order"], "album_name": "Substance",
                  "album_cover_url": "https://i.scdn.co/x",
                  "duration": 244720, "position": 1000},
        "paused": False, "stopped": False}

# 0. a live session fills the hold cache
daemon.go_status = lambda **_k: LIVE
st = orch.status()
assert st["spotify"]["track"] == "Blue Monday '88", st["spotify"]

# 1. mid-settle: trackless answer WITH pending_track_uri -> the card
#    holds the last track AND passes the pending uri through
daemon.go_status = lambda **_k: {"pending_track_uri": "spotify:track:b"}
st = orch.status()
assert st["spotify"]["track"] == "Blue Monday '88", \
    f"mid-burst card must hold the last track, got {st['spotify']}"
assert st["spotify"]["pending_track_uri"] == "spotify:track:b", \
    "the pending skip target must survive the card hold"
assert st["title"] == "Blue Monday '88", \
    f"the screen card must not read 'Nothing playing' mid-burst: {st['title']}"
print("1. trackless + pending skip: card held, pending passed through OK")

# 2. fully empty answer (transient timeout) still holds briefly (old rule)
daemon.go_status = lambda **_k: {}
st = orch.status()
assert st["spotify"]["track"] == "Blue Monday '88", st["spotify"]
print("2. fully empty answer: short hold still applies OK")

# 3. trackless answer with NO in-flight signal: no hold — a deliberate
#    stop must show immediately, not linger for the load window
daemon.go_status = lambda **_k: {"stopped": True}
st = orch.status()
assert st["spotify"]["track"] is None, \
    f"a signal-less trackless answer must NOT hold the card: {st['spotify']}"
print("3. trackless without any in-flight signal: card clears OK")

print("\nall spot_status_pending_hold checks passed")
