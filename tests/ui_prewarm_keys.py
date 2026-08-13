#!/usr/bin/env python3
"""Gate art prewarm cache-key correctness.

_prewarm_art decodes covers right after boot so the first carousel pass
doesn't stutter tile-by-tile. But the artwork cache is keyed on the `square`
flag, and the carousel/categories read 176 SQUARE (render_carousel,
render_cats) while prewarm warmed 176 fit — so the warm MISSED the carousel
entirely and it decoded a LANCZOS square crop inline on first traversal.
This gates that prewarm warms exactly the keys the views read."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")

from PIL import Image  # noqa: E402

import ui  # noqa: E402

LOCAL = os.path.join(tempfile.mkdtemp(), "cover.png")
Image.new("RGB", (500, 500), (120, 90, 160)).save(LOCAL)

app = ui.App.__new__(ui.App)
app.artwork_cache = {}
app._art_fails = {}
app._art_pending = set()
app.dirty = False
app.flat_entries = lambda: [{"image": LOCAL}, {"image": "http://x/remote.png"}]

ui.time.sleep = lambda *a, **k: None  # skip the 2s warmup + per-cover pacing
app._prewarm_art()

# the carousel + CATEGORY carousel read this exact variant (render_carousel:
# 1959, render_cats:1931) — it MUST be warm after prewarm
carousel_key = app._art_key(LOCAL, 176, square=True)
assert carousel_key in app.artwork_cache, \
    "prewarm must warm the 176 SQUARE key the carousel/categories read"
assert app.artwork_cache[carousel_key].size == (176, 176)
print("1. prewarm warms the 176 square carousel/category key OK")

# the now-playing window reads 128 SQUARE (render_now:1587) — also warmed
now_key = app._art_key(LOCAL, 128, square=True)
assert now_key in app.artwork_cache, \
    "prewarm must warm the 128 SQUARE key the now-playing view reads"
assert app.artwork_cache[now_key].size == (128, 128)
print("1b. prewarm warms the 128 square now-playing key OK")

# list rows read 56 fit (_row_art:946) — also warmed
row_key = app._art_key(LOCAL, 56, square=False)
assert row_key in app.artwork_cache, "prewarm must warm the 56 fit row key"
print("2. prewarm warms the 56 fit list-row key OK")

# the OLD (wrong) variant is not what the carousel reads — this is the bug
# that made the warm a no-op for the carousel
old_wrong = app._art_key(LOCAL, 176, square=False)
assert old_wrong != carousel_key
print("3. 176 fit != 176 square (the key namespacing that caused the miss) OK")

# remote covers are left to the async path — prewarm never blocks on http
assert not any(k[0].startswith("http") for k in app.artwork_cache), \
    "prewarm must skip http refs (async fetch owns those)"
print("4. prewarm skips remote (http) covers OK")

print("\nall ui_prewarm_keys checks passed")
