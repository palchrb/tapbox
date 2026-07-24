#!/usr/bin/env python3
"""Gate the spotify song picker (fork v0.1.1 GET /playlist/tracks).

Hold-Y in now-playing lists a playing playlist's songs — same picker as
podcasts — and picking one starts it via /play {id, episode:<track uri>}
-> player.py --episode -> /player/play {uri, skip_to_uri}. Contract:

- expand_target(tracks=True) maps the fork's listing to episode rows
  (id/url = track uri, title = "name — artists", image = cover url);
  sweep-pending entries (track: null) are skipped, not rendered blank.
- tracks stays OPT-IN: without it a spotify entry expands to the same
  leaf card as before — a browse tap must PLAY, never open a list.
- albums (HTTP 400) and a pre-v0.1.1 fork (404) degrade to the leaf
  card, no crash.
- play_spotify(start_uri=...) plays {uri, skip_to_uri=<pick>} from the
  top: no position, and the bookmark is neither read nor cleared."""
import io
import os
import sys
import tempfile
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import library, spotify  # noqa: E402

PL = "https://open.spotify.com/playlist/0hgSZmY9xhzx51hlLB2arI"
LISTING = {"uri": "spotify:playlist:0hgSZmY9xhzx51hlLB2arI",
           "snapshot_id": "abc", "length": 3, "cached": 2,
           "tracks": [
               {"uri": "spotify:track:a",
                "track": {"name": "Blue Monday '88",
                          "artist_names": ["New Order"],
                          "album_cover_url": "https://i.scdn.co/a"}},
               {"uri": "spotify:track:b", "track": None},  # sweep pending
               {"uri": "spotify:track:c",
                "track": {"name": "Shout", "artist_names": []}},
           ]}

asked = []
spotify.playlist_tracks = lambda uri, timeout=5: (asked.append(uri),
                                                  LISTING)[1]
library.spotify.playlist_tracks = spotify.playlist_tracks

# 1. tracks=True: fork listing -> picker rows; null-track rows skipped
r = library.expand_target(PL, name="80s", tracks=True)
assert r["kind"] == "spotify"
assert asked == ["spotify:playlist:0hgSZmY9xhzx51hlLB2arI"], asked
eps = r["episodes"]
assert [e["id"] for e in eps] == ["spotify:track:a", "spotify:track:c"], eps
assert eps[0]["title"] == "Blue Monday '88 — New Order", eps[0]
assert eps[0]["image"] == "https://i.scdn.co/a"
assert eps[1]["title"] == "Shout", eps[1]  # no artists: bare name
print("1. tracks=True: fork listing mapped to picker rows OK")

# 2. default (browse) expansion: unchanged leaf card, fork NOT queried
asked.clear()
r = library.expand_target(PL, name="80s")
assert r["episodes"] == [] and asked == [], (r["episodes"], asked)
print("2. tracks omitted: leaf card, fork not queried OK")

# 3. albums (400) / old fork (404) / api down: leaf card, no crash


def _boom(uri, timeout=5):
    raise urllib.error.HTTPError("u", 400, "not a playlist", {},
                                 io.BytesIO(b""))


library.spotify.playlist_tracks = _boom
r = library.expand_target(PL, tracks=True)
assert r["episodes"] == [], r["episodes"]
library.spotify.playlist_tracks = \
    lambda uri, timeout=5: (_ for _ in ()).throw(OSError("down"))
r = library.expand_target(PL, tracks=True)
assert r["episodes"] == [], r["episodes"]
print("3. album/old-fork/down: degrades to the leaf card OK")

# 4. play_spotify(start_uri): {uri, skip_to_uri}, from the top, bookmark
#    untouched
import player  # noqa: E402

BODY = {}
bookmark_calls = []
player.radio.touch_busy = lambda: None
player.radio.wait_paging_clear = lambda: None
player.spotify.status = lambda timeout=5: {"username": "u", "track": {}}
player.spotify.read_bookmark = \
    lambda uri: bookmark_calls.append(("read", uri))
player.spotify.clear_bookmark = \
    lambda uri: bookmark_calls.append(("clear", uri))


def _go(path, timeout=15, body=None):
    if path == "/player/play":
        BODY.update(body)
    return b"{}"


player.spotify.go = _go
player.play_spotify(PL, start_uri="spotify:track:c")
assert BODY.get("skip_to_uri") == "spotify:track:c", BODY
assert "position" not in BODY, f"a pick starts from the top: {BODY}"
assert bookmark_calls == [], \
    f"a pick must neither read nor clear the bookmark: {bookmark_calls}"
print("4. picked track: /player/play {uri, skip_to_uri}, bookmark alone OK")

print("\nall spot_track_picker checks passed")
