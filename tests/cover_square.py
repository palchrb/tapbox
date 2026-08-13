#!/usr/bin/env python3
"""Gate square cover art. Non-square source art (an NRK series/episode
with no squareImage falls back to a 16:9 banner) used to thumbnail to
~half the tile height and float in the slot — field 2026-07-20 'album
art halvparten så høy'. Cover contexts (carousel, now-playing) now
scale-to-cover and centre-crop to a filled square; letterbox-fit
(logos) is unchanged, and the two are cached under separate keys."""
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

WIDE = os.path.join(tempfile.mkdtemp(), "banner.png")
Image.new("RGB", (640, 360), (80, 140, 200)).save(WIDE)  # 16:9 like NRK

app = ui.App.__new__(ui.App)
app.artwork_cache = {}
app._art_fails = {}

# 1. square=True fills the tile (a filled NxN cover)
sq = app.artwork(WIDE, 176, square=True)
assert sq.size == (176, 176), sq.size
print("1. square cover fills the tile (176x176) OK")

# 2. the old fit path keeps aspect (this is what looked half-height) —
# proving the flag is what changed, not the source
fit = app.artwork(WIDE, 176, square=False)
assert fit.size[0] == 176 and fit.size[1] < 176, fit.size
print("2. fit (logos) still letterboxes to aspect OK")

# 3. square and fit are cached under DISTINCT keys — no collision, both
# retrievable
ks = app._art_key(WIDE, 176, square=True)
kf = app._art_key(WIDE, 176, square=False)
assert ks != kf, (ks, kf)
assert app.artwork_cache.get(ks).size == (176, 176)
assert app.artwork_cache.get(kf).size[1] < 176
print("3. square and fit covers cache under separate keys OK")

# 4. an already-square source is unchanged by square (no bogus crop)
SQSRC = os.path.join(tempfile.mkdtemp(), "album.png")
Image.new("RGB", (500, 500), (200, 100, 80)).save(SQSRC)
out = app.artwork(SQSRC, 128, square=True)
assert out.size == (128, 128), out.size
print("4. a square source stays square (no distortion) OK")

print("COVER SQUARE OK — non-square cover art fills the tile instead of "
      "showing at half height; logos are untouched.")
