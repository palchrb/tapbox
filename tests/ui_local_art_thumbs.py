#!/usr/bin/env python3
"""Local-source album art (podcast covers/episode images the sync
downloads) must hit the SAME disk thumb cache as remote art. Before
this, `if fetched:` only saved thumbs for http sources, so every UI
restart (= every deploy) re-decoded 1400-3000px originals — a
100-500ms placeholder flash per cover at 600MHz (energy audit
follow-up 2026-08-12). Pins: thumb written once, original never
touched again, mtime change refreshes, corrupt thumb self-heals."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
os.environ["VIBB_EMOJI"] = "0"

import ui  # noqa: E402
from PIL import Image  # noqa: E402

SRC = tempfile.mkdtemp()


def make_app():
    app = object.__new__(ui.App)
    app.artwork_cache = {}
    app._art_fails = {}
    return app


def big_cover(color, path):
    Image.new("RGB", (1400, 1400), color).save(path, "JPEG")


cover = os.path.join(SRC, "cover.jpg")
big_cover((200, 30, 30), cover)
app = make_app()

# 1. first decode writes a thumb to the ui-art disk cache
img = app.artwork(cover, size=176)
assert img is not None and max(img.size) == 176
thumbs = [n for n in os.listdir(ui.UI_ART_DIR) if n.endswith(".jpg")]
assert len(thumbs) == 1, thumbs
assert not [n for n in os.listdir(ui.UI_ART_DIR) if n.endswith(".part")]
print("1. local original decoded once, thumb persisted atomically OK")

# 2. a fresh app (UI restart) must serve from the thumb WITHOUT reading
#    the original: replace the original with garbage, keep its mtime
mt = os.path.getmtime(cover)
with open(cover, "wb") as f:
    f.write(b"not a jpeg at all")
os.utime(cover, (mt, mt))
img2 = make_app().artwork(cover, size=176)
assert img2 is not None and max(img2.size) == 176
assert img2.getpixel((88, 88))[0] > 150, "thumb should still be red"
print("2. after restart the thumb serves — original never re-read OK")

# 3. a re-synced cover (same path, NEW mtime) refreshes the thumb.
#    The key truncates to whole seconds, so bump mtime explicitly —
#    real resyncs are minutes apart, tests are not.
big_cover((30, 30, 200), cover)          # now blue
os.utime(cover, (mt + 5, mt + 5))
img3 = make_app().artwork(cover, size=176)
assert img3.getpixel((88, 88))[2] > 150, "new mtime must re-decode"
assert len([n for n in os.listdir(ui.UI_ART_DIR)
            if n.endswith(".jpg")]) == 2  # old thumb ages out via cap
print("3. mtime change re-keys the thumb — fresh art shows OK")

# 4. a corrupt thumb self-heals: next call deletes and rebuilds it
mt = os.path.getmtime(cover)
bad = ui._art_disk(cover, 176, False, mtime=mt)
with open(bad, "wb") as f:
    f.write(b"junk")
img4 = make_app().artwork(cover, size=176)
assert img4 is not None and img4.getpixel((88, 88))[2] > 150
with open(bad, "rb") as f:
    assert f.read(2) == b"\xff\xd8", "corrupt thumb was not rebuilt"
print("4. corrupt thumb deleted and rebuilt OK")

# 5. remote path untouched: an http ref still keys WITHOUT mtime, so
#    its disk path is stable across calls (regression guard for the
#    _art_disk signature change)
p1 = ui._art_disk("https://i.scdn.co/image/x", 176, False)
p2 = ui._art_disk("https://i.scdn.co/image/x", 176, False, mtime=None)
assert p1 == p2
print("5. remote disk keys unchanged by the signature change OK")

print("\nLOCAL ART THUMBS OK — decode once ever, refresh on resync.")
