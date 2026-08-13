#!/usr/bin/env python3
"""Gate offline-cache cleanup: prune_cache removes the episode dirs and
catalog files of entries no longer kept offline (deleted, or set to 'no
offline'), and never touches ones still wanted. Pure filesystem, no net."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = CACHE
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import content  # noqa: E402

KEEP_POD = "https://radio.nrk.no/podkast/fantorangenfortellinger"
KEEP_FEED = "https://example.com/rss.xml"
GONE_POD = "https://radio.nrk.no/podkast/oldstory"
feed_dir = content.feed_key(KEEP_FEED)


def touch(*parts):
    p = os.path.join(CACHE, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write("x")


# a wanted podcast, a wanted feed, an orphaned podcast, an orphaned feed
touch("fantorangenfortellinger", "e1.mp3")
touch("catalog-fantorangenfortellinger.json")
touch(feed_dir, "a.mp3")
touch("oldstory", "e9.mp3")
touch("catalog-oldstory.json")
touch("feed-deadbeef1234", "z.mp3")
# spotify cache lives elsewhere; a stray unrelated file must be left alone
touch("keep-me.txt")

removed = set(content.prune_cache([KEEP_POD, KEEP_FEED]))

assert os.path.isdir(os.path.join(CACHE, "fantorangenfortellinger")), "wanted pod pruned!"
assert os.path.exists(os.path.join(CACHE, "catalog-fantorangenfortellinger.json"))
assert os.path.isdir(os.path.join(CACHE, feed_dir)), "wanted feed pruned!"
print("1. entries still wanted keep their cache OK")

assert not os.path.exists(os.path.join(CACHE, "oldstory")), "orphan pod dir kept"
assert not os.path.exists(os.path.join(CACHE, "catalog-oldstory.json")), "orphan catalog kept"
assert not os.path.exists(os.path.join(CACHE, "feed-deadbeef1234")), "orphan feed kept"
assert {"oldstory", "catalog-oldstory.json", "feed-deadbeef1234"} <= removed, removed
print("2. orphaned dirs + catalog files pruned OK")

assert os.path.exists(os.path.join(CACHE, "keep-me.txt")), "unrelated file removed!"
print("3. unrelated top-level files left alone OK")

# empty keep-set (whole library deleted) clears every cache dir/catalog
content.prune_cache([])
left = [n for n in os.listdir(CACHE) if n != "keep-me.txt"]
assert left == [], f"cache not fully cleared: {left}"
print("4. empty library clears all offline caches OK")

print("CACHE PRUNE OK — deletes reclaim disk, keepers survive.")
