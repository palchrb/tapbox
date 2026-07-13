#!/usr/bin/env python3
"""Gate the now-playing transition polish: /status must never surface a
raw .mp3 filename or flash the show cover while mpv loads / advances a
track — the last published episode name+art bridges the gap (player.py
publishes the first item BEFORE mpv starts, and the poll loop is at most
one 3s tick behind on track changes)."""
import json
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

daemon.go_status = lambda: {}
orch = daemon.ORCH
orch._mpv_alive = lambda: True
orch.target, orch.source = "https://radio.nrk.no/podkast/show", "mpv"

E2 = "/cache/show/e2f00baa.mp3"
with open(daemon.NOW_FILE, "w") as f:
    json.dump({"id": "e2", "url": E2, "title": "Fantorangen og natta",
               "image": "/cache/show/e2f00baa.jpg", "paused": False,
               "duration": None, "target": orch.target}, f)

mpv = {"pause": False, "media-title": None, "playback-time": 0.5,
       "duration": None, "path": None}
daemon.mpv_get = lambda prop: mpv.get(prop)

# 1. mpv still loading (no path): the published name+art show already
st = orch.status()
assert st["title"] == "Fantorangen og natta", st["title"]
assert st["artwork"] == "/cache/show/e2f00baa.jpg"
print("1. while mpv loads: published episode name+art, no blank OK")

# 2. loaded, exact match: episode id resolves too
mpv["path"], mpv["media-title"] = E2, "e2f00baa.mp3"
st = orch.status()
assert st["episode_id"] == "e2" and st["title"] == "Fantorangen og natta"
print("2. exact match resolves episode id + title OK")

# 3. mpv advanced to the next file; publish is one poll behind — the
# queue map resolves the LIVE path instantly: NEW title+art, same second
with open(daemon.QUEUE_FILE, "w") as f:
    json.dump({"target": orch.target, "items": {
        E2: {"id": "e2", "title": "Fantorangen og natta",
             "image": "/cache/show/e2f00baa.jpg"},
        "/cache/show/9abc.mp3": {"id": "e3", "title": "Fantorangen og dagen",
                                 "image": "/cache/show/9abc.jpg"}}}, f)
mpv["path"], mpv["media-title"] = "/cache/show/9abc.mp3", "9abc.mp3"
st = orch.status()
assert st["title"] == "Fantorangen og dagen", \
    f"queue map not used: {st['title']}"
assert st["artwork"] == "/cache/show/9abc.jpg"
assert st["episode_id"] == "e3"
print("3. track change: queue map serves the NEW title+art instantly OK")

# 3b. a path OUTSIDE the queue map still bridges with the last publish
mpv["path"], mpv["media-title"] = "/cache/show/unknown.mp3", "unknown.mp3"
st = orch.status()
assert st["title"] == "Fantorangen og natta", \
    f"raw filename leaked: {st['title']}"
assert st["artwork"] == "/cache/show/e2f00baa.jpg", "art flashed away"
assert st["episode_id"] is None, "stale episode id kept"
print("3b. unknown path: filename suppressed, art bridged OK")

# 4. a REAL media-title (stream with metadata) is kept on mismatch
mpv["media-title"] = "NRK Super direkte"
st = orch.status()
assert st["title"] == "NRK Super direkte", st["title"]
print("4. a real stream title is never overridden OK")

# 5. a publish from a DIFFERENT target (stale file) is ignored
with open(daemon.NOW_FILE, "w") as f:
    json.dump({"id": "x", "url": "/other.mp3", "title": "Feil serie",
               "image": "/other.jpg", "target": "https://other/feed"}, f)
mpv["media-title"] = "9abc.mp3"
st = orch.status()
assert st["title"] != "Feil serie", "stale publish from another target used"
print("5. another target's stale publish is ignored OK")

print("NOW PLAYING OK — no filename flashes, art bridges track changes.")
