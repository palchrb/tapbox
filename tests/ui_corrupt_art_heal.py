#!/usr/bin/env python3
"""Gate corrupt-cache-art self-healing (field 2026-07-23: an episode jpg
truncated by a hard power cut failed EVERY decode with RuntimeError — and
the sync never refetched it because the file exists).

A LOCAL art file under CACHE_DIR that fails to decode is DELETED so the
next sweep refetches it. Scoped strictly to CACHE_DIR — a PWA-uploaded
logo outside it must never be auto-deleted."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = CACHE
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402

app = ui.App.__new__(ui.App)
app.artwork_cache = {}
app._art_fails = {}

# 1. a corrupt jpg under CACHE_DIR -> decode fails -> file is DELETED
corrupt = os.path.join(CACHE, "feed-x", "ep1.jpg")
os.makedirs(os.path.dirname(corrupt))
with open(corrupt, "wb") as f:
    f.write(b"\x00" * 100)  # not a jpeg
out = app.artwork(corrupt, 128, square=True)
assert out is None
assert not os.path.exists(corrupt), \
    "a corrupt CACHE_DIR art file must be deleted so the sweep refetches it"
print("1. corrupt cache art is deleted for refetch OK")

# 2. a corrupt file OUTSIDE CACHE_DIR (a parent-uploaded logo) is NEVER
#    auto-deleted — only backoff applies
outside = os.path.join(tempfile.mkdtemp(), "logo.png")
with open(outside, "wb") as f:
    f.write(b"\x00" * 100)
out = app.artwork(outside, 128)
assert out is None
assert os.path.exists(outside), \
    "art outside CACHE_DIR (user uploads) must never be auto-deleted"
print("2. art outside CACHE_DIR is never auto-deleted OK")

# 3. sanity: a VALID image still decodes and caches
from PIL import Image  # noqa: E402
good = os.path.join(CACHE, "feed-x", "ep2.jpg")
Image.new("RGB", (300, 300), (10, 20, 30)).save(good)
out = app.artwork(good, 128, square=True)
assert out is not None and out.size == (128, 128)
assert os.path.exists(good)
print("3. valid art decodes normally and survives OK")

print("\nall ui_corrupt_art_heal checks passed")
