#!/usr/bin/env python3
"""Gate the wake-snappiness fixes for an mpv podcast now-view.

(1) The episode art is RE-RESOLVED to the local cached .jpg at /status time,
so an episode that started while STREAMING (remote URL baked into
now-playing.json) still paints instantly on a later wake once the sweep has
cached it — never a wifi fetch. Read-time only; a missing local file keeps
the remote URL (never a dead local path).
(2) When the source is mpv the now-view is 100% local, so /status probes
go-librespot with a SHORT timeout instead of blocking the wake ~1.5s."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import content  # noqa: E402

orch = daemon.ORCH
orch._mpv_alive = lambda: True
orch.target, orch.source = "https://radio.nrk.no/podkast/show", "mpv"
REMOTE = "https://gfx.nrk.no/remote-episode-art.jpg"
E = "/cache/show/e2.mp3"

with open(daemon.NOW_FILE, "w") as f:
    json.dump({"id": "e2", "url": E, "title": "Episode 2", "image": REMOTE,
               "paused": True, "duration": 100, "target": orch.target}, f)
mpv = {"pause": True, "path": E, "media-title": "e2.mp3",
       "playback-time": 50, "duration": 100}
daemon.mpv_get = lambda p: mpv.get(p)
daemon.go_status = lambda **_k: {}

KEY = content.cache_key_for(orch.target)             # "show"
LOCAL = content._episode_image_file(KEY, "e2")       # CACHE/show/e2.jpg
os.makedirs(os.path.dirname(LOCAL), exist_ok=True)

# 1. local art present -> /status re-resolves to the LOCAL file (instant,
#    no wake-time wifi fetch) even though now-playing.json baked the URL
open(LOCAL, "w").close()
st = orch.status()
assert st["artwork"] == LOCAL, f"art must re-resolve to local, got {st['artwork']}"
print("1. episode art re-resolves to the local cached file OK")

# 2. no local file -> keep the remote URL (never a dead local path)
os.remove(LOCAL)
st = orch.status()
assert st["artwork"] == REMOTE, f"no local art -> keep remote, got {st['artwork']}"
print("2. missing local art keeps the remote url (no dead path) OK")

# 3. an mpv card probes go-librespot with the SHORT timeout, not 1.5s
seen = []
daemon.go_status = lambda **k: (seen.append(k.get("timeout")), {})[1]
orch.status()
assert seen and seen[-1] == daemon.GO_ST_MPV_TIMEOUT, \
    f"mpv card must use the short go_status timeout, got {seen}"
print("3. mpv now-view probes go-librespot with the short timeout OK")

print("\nall wake_mpv_art_local checks passed")
