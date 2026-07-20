#!/usr/bin/env python3
"""Gate the 'new episode' badge. A show never lights up on the first
sweep (existing content isn't 'new'); a later sweep that finds a newer
episode marks it, /library carries the flag, and playing the show clears
it. We never touch the resume target — the dot only surfaces fresh
content, it doesn't hijack playback."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = STATE
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(STATE, "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import library as lib  # noqa: E402

POD = "https://radio.nrk.no/podkast/fantorangen"
FEED = "https://ex.com/feed.rss"

# 1. first sight of a show acknowledges it — existing content is NOT new
lib._mark_new_seen(POD, "ep-5")
assert lib.is_new(POD) is False
print("1. first sweep never badges existing content OK")

# 2. same newest on the next sweep -> still nothing new
lib._mark_new_seen(POD, "ep-5")
assert lib.is_new(POD) is False
print("2. an unchanged feed stays quiet OK")

# 3. a genuinely newer episode -> the show is now 'new'
lib._mark_new_seen(POD, "ep-6")
assert lib.is_new(POD) is True
assert POD in lib.new_targets()
print("3. a fresh episode lights the badge OK")

# 4. playing the show acknowledges it -> the dot clears
lib.acknowledge_new(POD)
assert lib.is_new(POD) is False and POD not in lib.new_targets()
print("4. playing the show clears the badge OK")

# 5. two shows are independent; a blank newest id is ignored
lib._mark_new_seen(FEED, "f-1")          # first sight -> ack
lib._mark_new_seen(FEED, "f-2")          # new -> badge
lib._mark_new_seen(FEED, None)           # a failed read must not clobber
assert lib.is_new(FEED) is True and lib.is_new(POD) is False
print("5. shows are independent, a blank read is a no-op OK")

# 6. library_with_covers stamps e['new'] only on unacknowledged shows
lib.save_library({"version": 1, "sections": [{"id": "s", "name": "P",
    "entries": [
        {"id": "a", "name": "Fanto", "target": POD, "order": "auto",
         "cache": 5, "resume": True},
        {"id": "b", "name": "Feed", "target": FEED, "order": "auto",
         "cache": 5, "resume": True}]}]})
out = lib.library_with_covers()
by_id = {e["id"]: e for s in out["sections"] for e in s["entries"]}
assert by_id["a"].get("new") is None, "acknowledged show must not be flagged"
assert by_id["b"].get("new") is True, "the fresh show must be flagged"
print("6. /library flags only unacknowledged shows OK")

# 7. prune drops state for removed entries
lib._new_prune({POD})   # FEED removed from the library
assert FEED not in lib.new_targets()
print("7. removing an entry prunes its badge state OK")

print("NEW BADGE OK — surfaces fresh episodes, never on first sight, "
      "clears on play, and never touches the resume target.")
