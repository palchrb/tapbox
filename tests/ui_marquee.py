#!/usr/bin/env python3
"""Gate the menu marquee + artwork freshness:
- a too-long SELECTED label slides through its whole text (with resting
  pauses), short labels never scroll;
- a re-uploaded category logo (same path, new bytes) refreshes on the
  box instead of the old picture being cached forever."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")

import ui  # noqa: E402

# --- marquee ----------------------------------------------------------------
LONG = "Fantorangen og den utrolig lange episodetittelen"
real_mono = time.monotonic

# 1. short labels are returned as-is, no scrolling
txt, rolls = ui.marquee("Kort navn", 24)
assert (txt, rolls) == ("Kort navn", False)
print("1. short labels never scroll OK")

# 2. sweep a full period: starts at the start, reaches the END of the
# text, and every window is exactly maxlen chars
maxlen = 24
span = len(LONG) - maxlen
seen = set()
for step in range(span + 8):
    time.monotonic = lambda s=step: s * ui.MARQUEE_STEP_S + 0.01
    txt, rolls = ui.marquee(LONG, maxlen)
    assert rolls and len(txt) == maxlen
    seen.add(txt)
time.monotonic = real_mono
assert LONG[:maxlen] in seen, "never showed the start"
assert LONG[-maxlen:] in seen, "never reached the end of the name"
assert len(seen) == span + 1, "skipped positions"
print("2. the whole name slides past, both ends visible OK")

# 3. the resting pause: several consecutive steps hold the start window
time.monotonic = lambda: 0.01
first, _ = ui.marquee(LONG, maxlen)
time.monotonic = lambda: 3 * ui.MARQUEE_STEP_S + 0.01
still, _ = ui.marquee(LONG, maxlen)
time.monotonic = real_mono
assert first == still == LONG[:maxlen], "no resting pause at the start"
print("3. pauses at the start before sliding OK")

# --- artwork freshness --------------------------------------------------------
from PIL import Image  # noqa: E402
import tempfile  # noqa: E402


class FakeDisplay:
    on = True
    def show(self, img): pass
    def set_backlight(self, on): pass
    def set_brightness(self, b): pass


app = ui.App(FakeDisplay(), None)
logo = os.path.join(tempfile.mkdtemp(), "section-musikk.jpg")
Image.new("RGB", (60, 60), (200, 30, 30)).save(logo)   # red v1
os.utime(logo, (1000, 1000))
a1 = app.artwork(logo, 56)
assert a1.getpixel((10, 10))[0] > 150, "v1 should be red"

Image.new("RGB", (60, 60), (30, 200, 30)).save(logo)   # green v2, same path
os.utime(logo, (2000, 2000))
a2 = app.artwork(logo, 56)
assert a2.getpixel((10, 10))[1] > 150, "old logo stuck after re-upload"
assert len([k for k in app.artwork_cache if k[0] == logo]) == 1, \
    "stale cache entry not dropped"
print("4. re-uploaded logo refreshes (mtime-keyed cache) OK")

# 5. remote covers never block the render thread: artwork_async returns
# immediately and the image lands via a background fetch (+ repaint)
import io  # noqa: E402
import urllib.request  # noqa: E402

buf = io.BytesIO()
Image.new("RGB", (40, 40), (10, 10, 200)).save(buf, "JPEG")


class SlowResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): time.sleep(0.3); return buf.getvalue()


urllib.request.urlopen = lambda url, timeout=10: SlowResp()
app.dirty = False
t0 = real_mono()
assert app.artwork_async("http://art/cover.jpg", 176) is None
assert real_mono() - t0 < 0.2, "artwork_async blocked on the network"
for _ in range(100):  # the background fetch fills the cache
    if app.artwork_async("http://art/cover.jpg", 176) is not None:
        break
    time.sleep(0.05)
assert app.artwork_async("http://art/cover.jpg", 176) is not None, \
    "background fetch never landed"
assert app.dirty, "no repaint requested after the cover arrived"
print("5. remote covers load off-thread, render never blocks OK")

print("UI MARQUEE + ART OK — names readable, logos never stuck.")
