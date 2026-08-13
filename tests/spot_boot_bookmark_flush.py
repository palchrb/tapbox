#!/usr/bin/env python3
"""Gate the reboot/poweroff Spotify bookmark flush.

Symptom (field 2026-07-21): seek to the start of a song, reboot while it has
just begun playing, and boot-resume lands back at the OLD position. Root
cause: _spotify_bookmarker throttles disk writes (SD hygiene: 30s / on track
change), so a fresh position lives only in memory (bm_pending) and dies with
the thread at SIGTERM — leaving a stale on-disk bookmark for boot-resume.

Fix: the bookmarker mirrors the freshest bookmark into _SPOT_PENDING_BM every
tick, command() wakes it after any control (so a seek is captured within a
beat, not a 5s tick later), and _on_term flushes _SPOT_PENDING_BM to disk.
Playing-gated so a stopped/paused session is never resurrected."""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import spotify as s  # noqa: E402

orch = daemon.ORCH
orch.child = None
CTX = "spotify:playlist:6BLoPpXQXX6xBNisPVMWIN"
URL = "https://open.spotify.com/playlist/6BLoPpXQXX6xBNisPVMWIN"


def stat(pos, paused=False, track=True):
    return {"play_origin": "go-librespot", "paused": paused, "stopped": False,
            "track": ({"uri": "spotify:track:t", "position": pos,
                       "duration": 173000, "name": "T",
                       "artist_names": ["A"], "album_cover_url": None}
                      if track else None)}


def disk_pos():
    bm = s.read_bookmark(CTX)
    return bm["position"] if bm else None


# count disk writes without losing the real behaviour
_real_save = s.save_bookmark
writes = []
s.save_bookmark = lambda bm: (writes.append(bm["position"]), _real_save(bm))[1]

POS = {"p": 25000}
daemon.go_status = lambda **k: stat(POS["p"])
orch.source, orch.target = "spotify", URL

threading.Thread(target=daemon._spotify_bookmarker, daemon=True).start()


def tick():
    daemon._bm_wake.set()
    time.sleep(0.3)  # one iteration, then it blocks on the next wait


# 1. freshest position is kept in memory even while the disk write is throttled
tick()  # first playing tick: uri changed from None -> a real disk write at 25s
assert disk_pos() == 25000, "first playing tick should write the bookmark"
assert daemon._SPOT_PENDING_BM[0]["position"] == 25000

POS["p"] = 1000   # the user seeks back to the start
tick()            # same uri, < 30s -> the DISK write is throttled...
assert disk_pos() == 25000, "throttle must keep the disk bookmark at 25s"
assert daemon._SPOT_PENDING_BM[0]["position"] == 1000, \
    "...but the freshest position must be held in memory"
print("1. freshest position kept in memory while the disk write is throttled OK")

# 2. the shutdown flush persists that fresh position (the actual fix)
daemon._flush_spotify_bookmark()
assert disk_pos() == 1000, \
    "reboot/poweroff flush must persist the fresh (seek) position, not 25s"
print("2. reboot/poweroff flush persists the fresh position OK")

# park the bookmarker on an empty session so it stops touching state
POS_EMPTY = daemon.go_status
daemon.go_status = lambda **k: stat(0, track=False)
tick()
assert daemon._SPOT_LAST_PLAYING[0] is False

# 3. NOT playing -> flush is a no-op (a paused session already flushed; a
#    stopped one was cleared — neither may be resurrected)
_real_save(s.read_bookmark(CTX))  # ensure disk holds the known 1000
daemon._SPOT_PENDING_BM[0] = {"context_uri": CTX, "uri": "x", "position": 99999}
daemon._flush_spotify_bookmark()
assert disk_pos() == 1000, "a non-playing session must not be flushed/resurrected"
print("3. non-playing session is never flushed (no resurrection) OK")

# 4. stop() clears the in-memory state, so a reboot right after can't
#    resurrect the just-cleared bookmark
daemon.go = lambda *a, **k: b""
s.clear_bookmark = lambda *a, **k: None
daemon._spotify.clear_bookmark = s.clear_bookmark
daemon._SPOT_PENDING_BM[0] = {"context_uri": CTX, "uri": "x", "position": 5}
daemon._SPOT_LAST_PLAYING[0] = True
orch.stop()
assert daemon._SPOT_PENDING_BM[0] is None, "stop must drop the pending bookmark"
assert daemon._SPOT_LAST_PLAYING[0] is False, "stop must clear the playing flag"
daemon._flush_spotify_bookmark()  # must do nothing now
print("4. stop() clears the in-memory bookmark so it can't be resurrected OK")

print("\nall spot_boot_bookmark_flush checks passed")
