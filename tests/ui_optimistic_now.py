#!/usr/bin/env python3
"""The optimistic new-tile card + the disk-warm art shortcut.
Field 2026-08-12 (two-agent review): (B) after tapping a new tile the
now view showed the PREVIOUS tile's track for seconds — the daemon
commits its source switch late and /status truthfully describes the
old playback until then; (A) even a fully disk-cached remote cover
trailed its title by one repaint beat, because artwork_async deferred
ALL http refs to a thread. Pins: the tapped identity paints at once,
off-target /status is refused inside the window, on-target /status or
expiry wins, and a disk-warm http cover decodes inline with the
network hard-disabled."""
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


class FakeDisplay:
    on = True

    def show(self, img):
        pass

    def set_backlight(self, on):
        pass

    def set_brightness(self, b):
        pass


class FakeInputs:
    def poll(self, timeout):
        return []


posted = []
ui.api_post = lambda path, body, timeout=None: posted.append(
    (path, body)) or {}

app = ui.App(FakeDisplay(), FakeInputs())
OLD = {"target": "https://x/old", "title": "Gammelt spor",
       "artwork": "https://img/old.jpg", "playing": True}
ENT = {"id": "e1", "name": "Snipp Snapp Snute",
       "target": "https://x/new", "image": "/data/sss.jpg"}

# 1. the tap paints the tapped identity IMMEDIATELY
app.status = dict(OLD)
app.view = "carousel"
app._play_async({"id": ENT["id"]}, entry=ENT)
assert app.view == "now"
assert app.status["title"] == "Snipp Snapp Snute"
assert app.status["target"] == "https://x/new"
assert app.status["artwork"] == "/data/sss.jpg"
assert app.status["playing"] is True
print("1. tap paints the tapped entry at once OK")

# 2. the old source's swan song is REFUSED inside the window
app._set("status", dict(OLD))
assert app.status["title"] == "Snipp Snapp Snute", \
    "an off-target /status must not repaint the old track"
assert app._expect_target == "https://x/new"
print("2. off-target /status refused — old track never reappears OK")

# 3. the first ON-target status replaces the card and clears the guard
truth = {"target": "https://x/new", "title": "Askeladden (episode 3)",
         "artwork": "https://img/new.jpg", "playing": True}
app._set("status", dict(truth))
assert app.status["title"] == "Askeladden (episode 3)"
assert app._expect_target is None
app._set("status", dict(OLD))       # guard gone: normal adoption again
assert app.status["title"] == "Gammelt spor"
print("3. on-target truth adopted, guard cleared OK")

# 4. expiry: a play that never lands lets the OLD truth win again
app._play_async({"id": ENT["id"]}, entry=ENT)
app._expect_until = time.monotonic() - 1
app._set("status", dict(OLD))
assert app.status["title"] == "Gammelt spor", \
    "after the window the daemon's truth must win unconditionally"
assert app._expect_target is None
print("4. window expiry falls back to the daemon's truth OK")

# 5. a disk-warm http cover decodes INLINE — no thread, no network.
#    Seed the ui-art thumb, then make any network attempt explode.
REF = "https://i.scdn.co/image/deadbeef"
thumb = ui._art_disk(REF, 128, True)
os.makedirs(os.path.dirname(thumb), exist_ok=True)
Image.new("RGB", (128, 128), (200, 30, 30)).save(thumb, "JPEG")


class NoNet:
    def urlopen(self, *a, **k):
        raise AssertionError("network touched for a disk-warm cover")


app.artwork_cache.clear()
# ui imports urllib.request lazily (inside the fetch, off the boot path),
# so patch urlopen on the module itself — the same singleton the lazy
# import resolves to — not via a ui.urllib attribute that no longer exists.
import urllib.request  # noqa: E402
_real_urlopen = urllib.request.urlopen
urllib.request.urlopen = NoNet().urlopen
try:
    img = app.artwork_async(REF, 128, square=True)
finally:
    urllib.request.urlopen = _real_urlopen
assert img is not None and img.size == (128, 128)
assert img.getpixel((64, 64))[0] > 150
print("5. disk-warm remote cover decodes inline, zero network OK")

# 6. a genuinely cold http cover still defers (returns None, no block)
COLD = "https://i.scdn.co/image/coldcafe"
key = app._art_key(COLD, 128, True)
app._art_pending.add(key)           # pretend a fetch is already queued
assert app.artwork_async(COLD, 128, square=True) is None
app._art_pending.discard(key)
print("6. cold cover still deferred off the render thread OK")

print("\nOPTIMISTIC NOW OK — the screen shows what you tapped, "
      "and cached covers keep up with their titles.")
