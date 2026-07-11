#!/usr/bin/env python3
"""Gate per-episode resume: every episode remembers its own position, so
hopping between episodes continues each where it was left — not just the
last-played one. A finished episode is dropped so a re-tap starts fresh."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402  (reads TAPBOX_STATE at import)

KEY = "feed-abc"
A, B = "http://x/a.mp3", "http://x/b.mp3"      # stream URLs
A_LOCAL = "/var/lib/tapbox/cache/feed-abc/ida.mp3"  # cached form of A
IDA, IDB = "ida", "idb"

# listen into episode A, then into episode B
player.save_state(KEY, A, 300.0, IDA, duration=1800)
player.save_state(KEY, B, 120.0, IDB, duration=1800)
st = player.load_state(KEY)

# both positions survive independently (the old code kept only the last)
assert player.episode_pos(st, IDA, A) == 300.0, "episode A position lost"
assert player.episode_pos(st, IDB, B) == 120.0, "episode B position lost"
print("1. each episode keeps its own position OK")

# the same episode as a cached local file resolves to the same slot (id-keyed)
assert player.episode_pos(st, IDA, A_LOCAL) == 300.0
print("2. stream and cached URL share one position (keyed by id) OK")

# whole-feed bookmark still points at the last-played episode
assert st["id"] == IDB and st["pos"] == 120.0
print("3. whole-feed resume still tracks the last-played episode OK")

# finishing an episode drops its mid-position (re-tap starts fresh)
player.save_state(KEY, A, 1795.0, IDA, duration=1800)  # within RESUME_MIN_S of end
st = player.load_state(KEY)
assert player.episode_pos(st, IDA, A) == 0.0, "finished episode still resumes mid"
assert player.episode_pos(st, IDB, B) == 120.0, "unrelated episode disturbed"
print("4. a finished episode is cleared, others untouched OK")

# back-compat: an old-format state file (single bookmark, no episodes map)
old = {"url": B, "pos": 90.0, "id": IDB}
assert player.episode_pos(old, IDB, B) == 90.0, "old bookmark ignored"
assert player.episode_pos(old, IDA, A) == 0.0
print("5. legacy single-bookmark state still resumes OK")

# per-entry 'from start': the library accepts the flag and defaults to resume
from tapbox import library  # noqa: E402
lib = {"sections": [{"name": "S", "entries": [
    {"name": "Songs", "target": "https://ex.com/rss", "resume": False},
    {"name": "Story", "target": "https://radio.nrk.no/podkast/foo"}]}]}
out = library.normalize_library(lib)
assert out["sections"][0]["entries"][0]["resume"] is False
assert out["sections"][0]["entries"][1]["resume"] is True, "resume must default on"
lib["sections"][0]["entries"][0]["resume"] = "nope"
try:
    library.normalize_library(lib)
    raise AssertionError("non-bool resume should be rejected")
except ValueError:
    pass
print("6. per-entry resume flag validated + defaults on OK")

print("EPISODE RESUME OK — position remembered per episode, configurable.")
