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
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

daemon.go_status = lambda **_k: {}
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

# 6. a freshly tapped spotify target: go-librespot still describes the
# PREVIOUS context — /status must show the tapped entry's identity
# (bookmark name + cached mosaic), never the old track
from vibb import content, spotify as sp  # noqa: E402

orch._mpv_alive = lambda: False
NEW = "https://open.spotify.com/playlist/bbbbbbbbbbbbbbbbbbbbbb"
NEW_URI = "spotify:playlist:bbbbbbbbbbbbbbbbbbbbbb"
sp.save_bookmark({"context_uri": NEW_URI, "uri": "spotify:track:b3",
                  "position": 120000, "duration": 200000,
                  "name": "Regnvaersanger", "artists": ["Alf Proeysen"],
                  "artwork": "http://scdn/b3.jpg", "updated": 1})
mosaic = content.spotify_art_path(NEW)
os.makedirs(os.path.dirname(mosaic), exist_ok=True)
open(mosaic, "wb").write(b"jpg")
OLD_TRACK = {"uri": "spotify:track:OLD", "name": "Gammel sang",
             "artist_names": ["Forrige Artist"], "album_name": "Gammelt album",
             "position": 5000, "duration": 100000,
             "album_cover_url": "http://scdn/old.jpg"}
daemon.go_status = lambda **_k: {"track": dict(OLD_TRACK),
                            "paused": False, "stopped": False,
                            "play_origin": "go-librespot"}
orch._stop_child = lambda: None
orch._spawn = lambda *a, **k: None
orch.play(NEW)
st = orch.status()
assert st["title"] == "Regnvaersanger", f"old context leaked: {st['title']}"
assert st["artwork"] == mosaic, st["artwork"]
assert st["position"] == 120.0 and st["playing"] is True
# ...and the SUBTITLE too: the screen's artist line reads
# spotify.artists, which this guard used to leave describing the
# outgoing track — the new album's name above the previous album's
# artist (field 2026-08-02, carousel album -> album)
assert st["spotify"]["artists"] == ["Alf Proeysen"], \
    f"previous context's artist leaked: {st['spotify']['artists']}"
assert st["spotify"]["track"] == "Regnvaersanger", st["spotify"]["track"]
assert st["spotify"]["track_uri"] == "spotify:track:b3", \
    "the song picker must not mark the OLD context's row"
assert st["spotify"]["album"] != "Gammelt album", st["spotify"]["album"]
assert st["spotify"]["artwork"] == mosaic, st["spotify"]["artwork"]
print("6. new spotify tap shows ITS identity, not the old context OK")

# 6b. no bookmark yet (a never-played album): show NOTHING under the
# title rather than the previous album's artist
sp.clear_bookmark(NEW_URI)
orch.spot_pending = {"pre_uri": "spotify:track:OLD", "at": time.monotonic()}
st = orch.status()
assert st["spotify"]["artists"] == [], \
    f"a fresh album must not borrow an artist: {st['spotify']['artists']}"
assert st["spotify"]["track"] is None, st["spotify"]["track"]
sp.save_bookmark({"context_uri": NEW_URI, "uri": "spotify:track:b3",
                  "position": 120000, "duration": 200000,
                  "name": "Regnvaersanger", "artists": ["Alf Proeysen"],
                  "artwork": "http://scdn/b3.jpg", "updated": 1})
print("6b. never-played album: blank subtitle, never a borrowed artist OK")

# 7. ...and the moment the loaded track changes, live status takes over
daemon.go_status = lambda **_k: {"track": {"uri": "spotify:track:b3",
                                      "name": "Regnvaersanger (live)",
                                      "position": 1000, "duration": 200000,
                                      "album_cover_url": "http://scdn/b3.jpg"},
                            "paused": False, "stopped": False,
                            "play_origin": "go-librespot"}
st = orch.status()
assert st["title"] == "Regnvaersanger (live)", st["title"]
assert orch.spot_pending is None, "pending never cleared"
print("7. live track replaces the pending identity on load OK")

# 7b. offline-proof cover: a Spotify target carries artwork_local (the
# cached mosaic) alongside the remote album-art URL, so a reboot-resume
# with no net yet still shows SOMETHING instead of a blank card
assert st["artwork"] == "http://scdn/b3.jpg", st["artwork"]  # live remote
assert st["artwork_local"] == mosaic, st.get("artwork_local")
print("7b. spotify status carries the offline-proof cached mosaic OK")


# --- resume-position hold: the bar must stay on the bookmark while mpv
# --- loads-then-seeks, not flap 0:00 -> bookmark on every start/respawn

import time  # noqa: E402

orch._mpv_alive = lambda: True  # scenario 6 flipped this to False
orch.source = "mpv"
orch.target = "https://radio.nrk.no/podkast/show"
daemon.go_status = lambda **_k: {}
with open(daemon.NOW_FILE, "w") as f:
    json.dump({"id": "e2", "url": E2, "title": "Fantorangen og natta",
               "image": "/cache/show/e2f00baa.jpg", "paused": False,
               "duration": None, "resume_pos": 90.0,
               "target": orch.target}, f)
mpv.update(path=E2, duration=600.0)
orch.child_started = time.monotonic()  # just spawned

# 8. mpv ramping from 0 while it loads -> hold at the bookmark, not 0/1/2
for live in (0.0, 1.0, 42.0):
    mpv["playback-time"] = live
    assert orch.status()["position"] == 90.0, (live, "flapped")
print("8. loading ramp holds at the resume bookmark OK")

# 9. the seek lands (live reaches the target) -> track live smoothly
mpv["playback-time"] = 90.0
assert orch.status()["position"] == 90.0
mpv["playback-time"] = 93.0
assert orch.status()["position"] == 93.0, "did not release after the seek"
print("9. releases to live once the seek lands OK")

# 10. a fresh start (resume_pos below the threshold) never holds
with open(daemon.NOW_FILE, "w") as f:
    json.dump({"id": "e2", "url": E2, "title": "x", "image": None,
               "paused": False, "duration": None, "resume_pos": 0.0,
               "target": orch.target}, f)
mpv["playback-time"] = 2.0
assert orch.status()["position"] == 2.0
print("10. fresh start (no bookmark) reports live from 0 OK")

# 11. the hold can't freeze forever: past the settle window -> live
with open(daemon.NOW_FILE, "w") as f:
    json.dump({"id": "e2", "url": E2, "title": "x", "image": None,
               "paused": False, "duration": None, "resume_pos": 90.0,
               "target": orch.target}, f)
orch.child_started = time.monotonic() - daemon.POSITION_SETTLE_MAX_S - 1
mpv["playback-time"] = 3.0
assert orch.status()["position"] == 3.0, "still frozen past the window"
print("11. hold is bounded to the settle window OK")

# 12. tap->audio window: mpv is spawned but its IPC socket isn't up yet
# (mpv_get('pause') is None). Within the start grace, /status must show
# 'playing' from player.py's published intent, not a dead card.
with open(daemon.NOW_FILE, "w") as f:
    json.dump({"id": "e2", "url": E2, "title": "x", "image": None,
               "paused": False, "duration": None, "target": orch.target}, f)
mpv["pause"] = None                        # IPC not answering yet
orch.child_started = time.monotonic()      # just spawned
assert orch.status()["playing"] is True, "startup should show playing at once"
# a published pause (kid hit pause before mpv came up) is honored
with open(daemon.NOW_FILE, "w") as f:
    json.dump({"id": "e2", "url": E2, "title": "x", "image": None,
               "paused": True, "duration": None, "target": orch.target}, f)
assert orch.status()["playing"] is False, "published pause must win"
# past the grace, a dead IPC is NOT optimistically 'playing'
orch.child_started = time.monotonic() - daemon.MPV_START_GRACE_S - 1
assert orch.status()["playing"] is False, "no optimism past the grace window"
mpv["pause"] = False
print("12. tap->audio: startup shows playing at once, bounded by the grace OK")

print("NOW PLAYING OK — no filename flashes, art bridges track changes, "
      "position holds steady on the bookmark.")
