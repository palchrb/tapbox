#!/usr/bin/env python3
"""A storytel series expands to its DOWNLOADED books, and prune keeps them.

The integration points into content.py / library.py, each with a way to
fail silently and expensively:

  - expand_entries on a storytel target returns one row per DOWNLOADED
    book, in reading order, keyed by consumableId — and books not yet on
    disk are OMITTED, never streamed from an expiring signed url, so the
    branch touches the network zero times (the playback path must be
    offline-clean, like a cached feed);
  - cache_key_for returns a non-None key so prune_cache KEEPS the
    download dir. Return None and shutil.rmtree deletes gigabytes of
    audiobooks on the next PUT /library — the one mistake in this whole
    feature that is both silent and catastrophic;
  - collection_image reads a local cover only, never the network;
  - _natural_order says oldest_first, so a series plays book 1 first
    rather than the last book bought.

Nothing here touches the network: content._online is replaced with a
raiser to PROVE the storytel path never probes."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
CACHE = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = CACHE
os.environ["VIBB_STATE"] = tempfile.mkdtemp()

from vibb import content, storytel, library  # noqa: E402

# any network probe from the storytel path is a bug: make it explode
content._online = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("the storytel path must never probe connectivity"))

TARGET = "storytel:series:26175"
BOOKS = [
    {"consumable_id": "160661", "title": "Godteristøvsugeren", "order": 1,
     "duration_ms": 721893},
    {"consumable_id": "160662", "title": "Tomattorsken", "order": 2,
     "duration_ms": 700000},
    {"consumable_id": "160663", "title": "Operaslangen", "order": 3,
     "duration_ms": 690000},
]
storytel.write_shelf(TARGET, "Kokosbananas", BOOKS)
d = storytel.cache_dir(TARGET)

# download books 1 and 3 only (2 is mid-sweep, not on disk yet)
for cid in ("160661", "160663"):
    with open(os.path.join(d, f"{cid}.mp3"), "wb") as f:
        f.write(b"\xff\xfb\x90\x64fake mpeg")
with open(os.path.join(d, "cover.jpg"), "wb") as f:
    f.write(b"jpeg")
with open(os.path.join(d, "160663.jpg"), "wb") as f:
    f.write(b"per-book jpeg")

# 1. expand returns ONLY the downloaded books, in reading order, keyed by
#    consumableId, with local mp3 paths — and book 2 is omitted, not
#    streamed
rows = content.expand_entries(TARGET)
assert [r["id"] for r in rows] == ["160661", "160663"], rows
assert all(r["url"].endswith(".mp3") and os.path.exists(r["url"])
           for r in rows), "every url must be a real local file"
assert [r["title"] for r in rows] == ["Godteristøvsugeren", "Operaslangen"]
print("1. expand returns only downloaded books, ordered, keyed by id OK")

# 2. per-book art beats the series cover; a book without its own jpg falls
#    back to cover.jpg
by_id = {r["id"]: r for r in rows}
assert by_id["160663"]["image"].endswith("160663.jpg"), "per-book art wins"
assert by_id["160661"]["image"].endswith("cover.jpg"), "fallback to the cover"
print("2. per-book cover wins, series cover is the fallback OK")

# 3. THE CRITICAL ONE: cache_key_for must claim the dir, and it must match
#    the basename storytel actually downloaded into, or prune deletes it
key = content.cache_key_for(TARGET)
assert key == os.path.basename(d), (key, os.path.basename(d))
kept = content.prune_cache([TARGET])       # this target is still in the library
assert os.path.exists(os.path.join(d, "160661.mp3")), \
    "prune must KEEP a downloaded book whose target is in the library"
assert os.path.basename(d) not in kept, "the dir must not be removed"
print("3. prune_cache keeps the download dir for a library target OK")

# 4. and prune DOES remove it once the target leaves the library — no
#    orphan gigabytes, but only then
content.prune_cache([])                     # nothing wants it anymore
assert not os.path.exists(d), "an orphaned storytel dir must be pruned"
print("4. prune removes the dir once no entry wants it OK")

# 5. collection_image is a local read; with the dir gone it is None, and
#    it never probed anything
assert content.collection_image(TARGET) is None
storytel.write_shelf(TARGET, "Kokosbananas", BOOKS)   # dir back, no cover
assert content.collection_image(TARGET) is None, "no cover file -> None"
with open(os.path.join(storytel.cache_dir(TARGET), "cover.jpg"), "wb") as f:
    f.write(b"jpeg")
assert content.collection_image(TARGET).endswith("cover.jpg")
print("5. collection_image reads a local cover only OK")

# 6. a book series plays book 1 first
assert library._natural_order(TARGET) == "oldest_first"
print("6. a storytel series is oldest_first (book 1 first) OK")

# 7. an empty / never-synced target expands to nothing, and never raises
assert content.expand_entries("storytel:series:00000") == []
assert storytel.newest_book_id("storytel:series:00000") is None
print("7. an unsynced target expands to nothing, cleanly OK")

# 8. newest_book_id (the 'new content' badge) is the last book by order
assert content.newest_episode_id(TARGET) == "160663"
print("8. newest_episode_id is the last book in the series OK")

print("\nSTORYTEL EXPAND OK — a series is its downloaded books in order, "
      "the download dir survives prune, and the play path never dials out.")
