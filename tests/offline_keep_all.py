#!/usr/bin/env python3
"""Gate the 'keep all' offline option (cache == -1): the library accepts
the sentinel, the sweeper schedules it, and content.sync downloads every
episode instead of the newest N. Pure stubs — no network, no ffmpeg."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import content, library  # noqa: E402

# 1. library validation: -1 (all) allowed, out-of-range rejected
lib = {"sections": [{"name": "S", "entries": [
    {"name": "Pod", "target": "https://radio.nrk.no/podkast/foo", "cache": -1}]}]}
out = library.normalize_library(lib)
assert out["sections"][0]["entries"][0]["cache"] == -1
for bad in (-2, 101):
    lib["sections"][0]["entries"][0]["cache"] = bad
    try:
        library.normalize_library(lib)
        raise AssertionError(f"cache={bad} should be rejected")
    except ValueError:
        pass
print("1. library accepts -1, rejects -2/101 OK")

# 2. the sweeper schedules 'keep all' entries (n != 0), skips n == 0
assert library._sync_args_for("https://radio.nrk.no/podkast/foo", -1) \
    == ["sync", "foo", "-1", "podcast"]
print("2. sweeper builds sync args for keep-all OK")

# 3. content.sync: count<0 downloads EVERY episode, N caps to newest N
EPISODES = [{"id": f"e{i}", "url": f"http://x/{i}.mp3"} for i in range(20)]
content._catalog = lambda slug, kind: list(EPISODES)   # oldest first
content._catalog_image = lambda slug, kind: None
content._episode_file = lambda slug, i, kind: os.path.join(
    os.environ["TAPBOX_CACHE"], slug, f"{i}.x")
got = []
content._download = lambda url, dest, **k: got.append(dest)

got.clear()
content.sync("foo", count=5, kind="podcast")
assert len(got) == 5, f"keep 5 -> {len(got)} downloads"

got.clear()
content.sync("foo", count=-1, kind="podcast")
assert len(got) == len(EPISODES), f"keep all -> {len(got)}/{len(EPISODES)}"
print("3. content.sync: keep-5 caps, keep-all grabs everything OK")

print("OFFLINE KEEP-ALL OK — sentinel plumbed end to end.")
