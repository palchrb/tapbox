#!/usr/bin/env python3
"""Gate the episode/track picker's row art (56px, top-right corner) —
the same affordance the entries menus have had all along. Contract:

- row 0 ("Play all") and rows without their own image show the
  collection cover; other rows show their own.
- the fetch goes through artwork_async — spotify picker rows carry
  REMOTE cover urls and a list scroll must never block the render
  thread on the network (the carousel's A3 rule).
- while scrolling (<0.4s since input) an uncached cover is deferred
  (returns None, marks dirty) so the network fetch starts only when
  the cursor settles."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402

app = ui.App.__new__(ui.App)
app.artwork_cache = {}
app.dirty = False
app.last_input = time.monotonic() - 10  # settled long ago
app.expanded = {"image": "https://i.scdn.co/collection",
                "episodes": [{"id": "t1", "image": "https://i.scdn.co/t1"},
                             {"id": "t2", "image": None}]}
calls = []
app.artwork_async = lambda ref, size=110, square=False: \
    (calls.append((ref, size)), "IMG")[1]

# 1. row 0 = Play all -> collection cover, via artwork_async at 56px
app.sel = 0
assert app.episode_art() == "IMG"
assert calls == [("https://i.scdn.co/collection", 56)], calls
print("1. play-all row: collection cover via artwork_async OK")

# 2. a track row with its own cover shows it
calls.clear()
app.sel = 1
app.episode_art()
assert calls == [("https://i.scdn.co/t1", 56)], calls
print("2. track row: its own cover OK")

# 3. a row without art falls back to the collection cover
calls.clear()
app.sel = 2
app.episode_art()
assert calls == [("https://i.scdn.co/collection", 56)], calls
print("3. artless row: collection cover fallback OK")

# 4. mid-scroll + uncached: deferred (None + dirty), no fetch call
calls.clear()
app.last_input = time.monotonic()  # scrolling right now
app.dirty = False
app.sel = 1
assert app.episode_art() is None and app.dirty is True
assert calls == [], f"no fetch while scrolling: {calls}"
print("4. mid-scroll uncached: deferred, no fetch OK")

# 5. nothing to show: no expanded image, no row image
app.last_input = time.monotonic() - 10
app.expanded = {"episodes": [{"id": "x", "image": None}]}
app.sel = 1
assert app.episode_art() is None
print("5. no art anywhere: None OK")

print("\nall ui_episode_row_art checks passed")
