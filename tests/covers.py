#!/usr/bin/env python3
"""Gate menu artwork: Spotify entries get a cover via oEmbed (cached on
disk, fetched only by the sweeper, orphans dropped), and sections can
carry an uploaded home-screen logo that survives validation and never
falls victim to the offline-cache pruner."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_ART"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import content, library  # noqa: E402

CACHE = os.environ["TAPBOX_CACHE"]
PL = "https://open.spotify.com/playlist/0hgSZmY9xhzx51hlLB2arI?si=x"
PL2 = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
POD = "https://radio.nrk.no/podkast/fantorangen"

# stub the network: oEmbed JSON + thumbnail download
fetched = []
content._get = lambda url, timeout=8: json.dumps(
    {"thumbnail_url": "https://i.scdn.co/image/mosaic123"}).encode()


def fake_download(url, dest, size=300):
    fetched.append(url)
    with open(dest, "wb") as f:
        f.write(b"jpegdata")


content._download_image = fake_download

# 1. ensure_spotify_art fetches ONLY spotify targets, once
content.ensure_spotify_art([PL, PL2, POD])
assert len(fetched) == 2, fetched
assert os.path.exists(content.spotify_art_path(PL))
content.ensure_spotify_art([PL, PL2, POD])  # second sweep: all cached
assert len(fetched) == 2, "re-fetched already-cached art"
print("1. spotify art fetched once per entry, podcasts untouched OK")

# 2. collection_image serves the cached file (no network)
content._get = content._download = None  # any call would explode
assert content.collection_image(PL) == content.spotify_art_path(PL)
assert content.collection_image("spotify:playlist:unknown123") is None
print("2. collection_image returns the cached spotify cover OK")

# 3. the offline-cache pruner must NOT delete the art dir
removed = content.prune_cache([])  # empty library: everything else goes
assert os.path.exists(content.spotify_art_path(PL)), "pruner ate the art"
assert content.SPOTIFY_ART_DIR not in removed
print("3. prune_cache leaves spotify-art alone OK")

# 4. ...ensure_spotify_art drops art for entries no longer in the library
def fake_dl2(url, dest, size=300):
    with open(dest, "wb") as f:
        f.write(b"jpegdata")
content._get = lambda url, timeout=8: json.dumps(
    {"thumbnail_url": "x"}).encode()
content._download_image = fake_dl2
content.ensure_spotify_art([PL2])
assert not os.path.exists(content.spotify_art_path(PL)), "orphan art kept"
assert os.path.exists(content.spotify_art_path(PL2))
print("4. orphaned spotify art removed OK")

# 5. sections validate an optional image (short string), reject junk
lib = {"sections": [{"name": "Musikk", "image": "/var/lib/tapbox/art/s.jpg",
                     "entries": [{"name": "P", "target": PL}]}]}
out = library.normalize_library(lib)
assert out["sections"][0]["image"] == "/var/lib/tapbox/art/s.jpg"
lib["sections"][0]["image"] = ["not", "a", "string"]
try:
    library.normalize_library(lib)
    raise AssertionError("non-string section image should be rejected")
except ValueError:
    pass
lib["sections"][0].pop("image")
assert "image" not in library.normalize_library(lib)["sections"][0]
print("5. section logo field validated, optional OK")

# 6. episode images: sync caches one per kept episode, and expansion
# then serves the LOCAL file (now-playing shows the episode's own
# picture offline); un-synced episodes keep their remote URL
imgs = []
content._download_image = lambda url, dest, size=300: (
    imgs.append(url), open(dest, "wb").write(b"jpg"))
content._catalog = lambda slug, kind: [
    {"id": "e1", "url": "http://x/1.mp3", "title": "En",
     "image": "http://img/1.jpg"},
    {"id": "e2", "url": "http://x/2.mp3", "title": "To",
     "image": "http://img/2.jpg"}]
content._episode_file = lambda slug, i, kind: os.path.join(
    CACHE, slug, f"{i}.mp3")
content._download = lambda url, dest, **k: open(dest, "wb").write(b"mp3")
content.sync("show", count=1, kind="podcast")   # keep newest 1 (= e2)
assert imgs == ["http://img/2.jpg"], imgs
assert os.path.exists(content._episode_image_file("show", "e2"))
assert not os.path.exists(content._episode_image_file("show", "e1"))
content.sync("show", count=1, kind="podcast")   # second sync: no refetch
assert len(imgs) == 1, "episode image re-downloaded"
print("6. sync caches per-episode artwork once, only for kept episodes OK")

entries = content._queue("show", "podcast", newest_first=True)
by_id = {e["id"]: e for e in entries}
assert by_id["e2"]["image"] == content._episode_image_file("show", "e2")
assert by_id["e1"]["image"] == "http://img/1.jpg", by_id["e1"]
print("7. expansion serves the cached episode image, remote for the rest OK")

# 8. shrink_covers: old full-size covers on disk get downscaled once,
# already-small ones are left untouched (decoding 3000px art per tile
# made the carousel's first pass crawl)
from PIL import Image  # noqa: E402
big = os.path.join(CACHE, "bigshow", "cover.jpg")
small = os.path.join(CACHE, "smallshow", "cover.jpg")
os.makedirs(os.path.dirname(big)), os.makedirs(os.path.dirname(small))
Image.new("RGB", (2000, 2000)).save(big, "JPEG")
Image.new("RGB", (300, 300)).save(small, "JPEG")
small_mtime = os.path.getmtime(small)
assert content.shrink_covers() == 1
assert max(Image.open(big).size) <= 300, "big cover not shrunk"
assert os.path.getmtime(small) == small_mtime, "small cover rewritten"
assert content.shrink_covers() == 0, "second pass must be a no-op"
print("8. oversized covers shrunk once, small ones untouched OK")

print("COVERS OK — spotify mosaics cached, section logos validated.")
