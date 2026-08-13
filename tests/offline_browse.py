#!/usr/bin/env python3
"""Gate offline-first browsing: opening a fully-cached RSS feed while the
network is down must serve the cached listing INSTANTLY (zero network I/O),
not block on a doomed fetch that only times out and falls back to the same
cache. Regression for 'Fetching episodes ... -> network error -> retry' on a
feed whose every episode was already downloaded offline."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import content  # noqa: E402

CACHE = os.environ["VIBB_CACHE"]
TARGET = "https://feeds.acast.com/public/shows/deadbeef"
KEY = content.feed_key(TARGET)
A, B = "http://cdn/a.mp3", "http://cdn/b.mp3"

# a synced feed: listing on disk + episode A downloaded, B not
os.makedirs(os.path.join(CACHE, KEY), exist_ok=True)
with open(os.path.join(CACHE, KEY, "feed.json"), "w") as f:
    json.dump({"image": "http://cdn/cover.jpg",
               "items": [[A, "Ep A", None], [B, "Ep B", None]]}, f)
a_local = os.path.join(CACHE, KEY, f"{content._feed_episode_id(A)}.mp3")
open(a_local, "wb").close()

# any network touch while offline is a bug — make it explode
def _boom(*a, **k):
    raise AssertionError("network used while offline")

content._get = _boom
content._sniffs_like_feed = _boom

# 1. offline browse: cached listing served, cached episode -> local path
content._online = lambda: False
out = content.expand_entries(TARGET)
assert [e["title"] for e in out] == ["Ep A", "Ep B"], out
assert out[0]["url"] == a_local, "cached episode should resolve to local file"
assert out[1]["url"] == B, "un-cached episode should keep its stream URL"
print("1. offline browse serves cached listing, no network OK")

# 2. STALE_OK (playback) does the same even if _online() would say True
content._online = lambda: (_ for _ in ()).throw(
    AssertionError("_online must not be consulted under STALE_OK"))
content.STALE_OK = True
out = content.expand_entries(TARGET)
assert out[0]["url"] == a_local
content.STALE_OK = False
print("2. playback (STALE_OK) stays offline-first, skips the probe OK")

# 3. online browse DOES refresh from the network (and rewrites feed.json)
seen = {}
content._online = lambda: True
content._get = lambda url, **k: seen.setdefault("hit", url) or b"<rss/>"
content._parse_feed = lambda b: ("http://cdn/cover2.jpg",
                                 [(A, "Ep A2", None), (B, "Ep B2", None)])
out = content.expand_entries(TARGET)
assert seen.get("hit") == TARGET, "online browse must fetch the feed"
assert [e["title"] for e in out] == ["Ep A2", "Ep B2"], out
with open(os.path.join(CACHE, KEY, "feed.json")) as f:
    assert json.load(f)["items"][0][1] == "Ep A2", "fresh listing not persisted"
print("3. online browse refreshes + repersists the listing OK")

print("OFFLINE BROWSE OK — cached feeds open instantly offline, refresh online.")
