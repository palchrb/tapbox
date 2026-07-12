#!/usr/bin/env python3
"""Gate Spotify resume bookkeeping: while the box plays, every tick must
snapshot track+position into a PER-CONTEXT bookmark, and nothing else may
touch it. Regressions for the three ways 'it started from the top again'
happened in the field:
  - a phone streaming its own music through the box (Spotify Connect)
    stamped the box context over the phone's track/position,
  - two playlist cards shared ONE bookmark file, wiping each other,
  - (in player.py) a slow track load silently skipped the resume seek."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import spotify as s  # noqa: E402

PL_A = "spotify:playlist:aaaaaaaaaaaaaaaaaaaaaa"
PL_B = "spotify:playlist:bbbbbbbbbbbbbbbbbbbbbb"


def st(uri="spotify:track:t1", pos=90000, origin="go-librespot",
       paused=False, stopped=False, track=True):
    return {"play_origin": origin, "paused": paused, "stopped": stopped,
            "track": {"uri": uri, "position": pos, "duration": 200000,
                      "name": "T", "artist_names": ["A"],
                      "album_cover_url": None} if track else None}


# 1. a box-initiated playing session is bookkept (position passes through)
bm = s.bookmark_step(st(pos=123456), PL_A)
assert bm and bm["uri"] == "spotify:track:t1" and bm["position"] == 123456
assert bm["context_uri"] == PL_A
print("1. box-initiated playback is bookkept OK")

# 2. paused / stopped / empty sessions leave the bookmark alone
assert s.bookmark_step(st(paused=True), PL_A) is None
assert s.bookmark_step(st(stopped=True), PL_A) is None
assert s.bookmark_step(st(track=False), PL_A) is None
print("2. paused/stopped/empty ticks do not write OK")

# 3. THE PHONE-CLOBBER GUARD: a Connect session started from a phone
# carries the phone's play_origin — it must NOT overwrite the box bookmark
assert s.bookmark_step(st(uri="spotify:track:phone", origin="playlist"),
                       PL_A) is None
print("3. a phone-driven session cannot clobber the bookmark OK")

# 4. back-compat: an older go-librespot without play_origin still bookkeeps
assert s.bookmark_step(st(origin=None), PL_A) is not None
assert s.bookmark_step(st(origin=""), PL_A) is not None
print("4. missing play_origin (old binary) still bookkeeps OK")

# 5. no known context -> nothing to resume against later -> no write
assert s.bookmark_step(st(), None) is None
print("5. context-less playback is not bookmarked OK")

# 6. PER-CONTEXT: playlists A and B keep independent positions
s.save_bookmark(s.bookmark_step(st(uri="spotify:track:a3", pos=300000), PL_A))
s.save_bookmark(s.bookmark_step(st(uri="spotify:track:b7", pos=70000), PL_B))
a, b = s.read_bookmark(PL_A), s.read_bookmark(PL_B)
assert a["uri"] == "spotify:track:a3" and a["position"] == 300000, a
assert b["uri"] == "spotify:track:b7" and b["position"] == 70000, b
print("6. two playlists keep independent bookmarks OK")

# 7. legacy migration: the old single-file bookmark is still readable
# for a context with no per-context file yet
legacy_ctx = "spotify:album:cccccccccccccccccccccc"
with open(s.LEGACY_BM_FILE, "w") as f:
    json.dump({"context_uri": legacy_ctx, "uri": "spotify:track:old",
               "position": 45000}, f)
lg = s.read_bookmark(legacy_ctx)
assert lg and lg["uri"] == "spotify:track:old", lg
# ...but a per-context file wins over the legacy one
assert s.read_bookmark(PL_A)["uri"] == "spotify:track:a3"
print("7. legacy single-file bookmark still readable, new files win OK")

# 8. clear_bookmark forgets ONE context (stop / --fresh), others survive
s.clear_bookmark(PL_A)
assert s.read_bookmark(PL_A) is None or \
    s.read_bookmark(PL_A).get("uri") != "spotify:track:a3"
assert s.read_bookmark(PL_B)["uri"] == "spotify:track:b7", "B was wiped too"
print("8. clearing one context leaves the other playlist's position OK")

# 9. logout forgets everything
s.clear_all_bookmarks()
assert s.read_bookmark(PL_B) is None
assert not os.path.exists(s.LEGACY_BM_FILE)
print("9. logout clears every bookmark OK")

print("SPOTIFY RESUME OK — per-context bookmarks, phone can't corrupt them.")
