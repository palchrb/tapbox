#!/usr/bin/env python3
"""Gate that the category carousel never blocks on a remote logo.

render_carousel fetches remote covers off-thread via artwork_async, but
render_cats used a synchronous artwork() call — so a category whose image is
an http URL blocked the render thread on urlopen(timeout=4) (QA A3). This
gates parity: a remote category logo goes through artwork_async (off-thread),
never the synchronous artwork() on the render thread."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")

from PIL import Image, ImageDraw  # noqa: E402

import ui  # noqa: E402


class FakeDisplay:
    on = True
    def show(self, img): pass
    def set_backlight(self, on): pass
    def set_brightness(self, b): pass


REMOTE = "http://ex.com/logo.png"

app = ui.App(FakeDisplay(), None)
app._lib_at = time.monotonic() + 999
app.settings = {"simple_nav": 2}
app.system = {}
app.cat_sel = 0
app.library = {"sections": [
    {"id": "c", "name": "Cat", "image": REMOTE,
     "entries": [{"id": "e", "name": "E", "target": "spotify:playlist:a"}]}]}

# record which art path each ref takes, and isolate the drawing sub-calls
seen = {"async": [], "sync": []}
app.artwork_async = lambda ref, size, square=False: (
    seen["async"].append(ref), None)[1]
app.artwork = lambda ref, size, square=False: (
    seen["sync"].append(ref), None)[1]
app._cover_tile = lambda d, img, art, name, new=False: (name, 0)
app._volume_overlay = lambda d: None
app._bt_overlay = lambda d: None

img = Image.new("RGB", (240, 240))
app.render_cats(ImageDraw.Draw(img), img)

assert REMOTE in seen["async"], \
    "render_cats must route a remote logo through artwork_async (off-thread)"
assert REMOTE not in seen["sync"], \
    "render_cats must NOT block on a synchronous fetch for a remote logo"
print("1. category carousel routes remote logos through artwork_async OK")

print("\nall ui_cats_async_art checks passed")
