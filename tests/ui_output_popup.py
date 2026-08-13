#!/usr/bin/env python3
"""Gate the hold-X output-switch confirmation. It used to be a blocking
full-screen draw_message + time.sleep(1.2); now it's the SAME transient
rounded-box popup the speaker/net overlays use (cosmetic parity, field
ask 2026-07-21) — non-blocking, self-clearing when output_flash expires.
The message content is preserved: which speaker, plus the no-sound-card
warning when the target has no card."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")  # no SPI in tests

import ui  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

POSTS = []
CUR = {"output": "bt"}
WARN = {"on": False}


def fake_get(path, timeout=10):
    if path == "/output":
        return {"output": CUR["output"]}
    return {}


def fake_post(path, body=None, timeout=15):
    POSTS.append((path, body))
    return {"warning": "no card"} if WARN["on"] else {}


ui.api_get = fake_get
ui.api_post = fake_post

app = object.__new__(ui.App)
app.output_flash = 0.0
app.output_shown = ""
app.output_warning = False
app.dirty = False
# a blocking full-screen message would call draw_message — record any use
# so we can prove the switch no longer blocks the render loop
BLOCKED = []
app.draw_message = lambda *a, **k: BLOCKED.append(a)


def blank():
    img = Image.new("RGB", (ui.W, ui.H), (0, 0, 0))
    return img, ImageDraw.Draw(img)


# 1. hold-X flips bt -> local, posts the switch, and arms the popup
# WITHOUT blocking (no draw_message, flash set into the future)
t0 = time.monotonic()
app._toggle_output()
assert POSTS == [("/output", {"device": "local"})], POSTS
assert app.output_shown == "Built-in speaker", app.output_shown
assert app.output_warning is False
assert app.output_flash > t0, "popup must be armed into the future"
assert app.dirty is True, "an instant repaint must be requested"
assert BLOCKED == [], "the switch must not block on a full-screen message"
print("1. hold-X flips output, arms the popup, never blocks OK")

# 2. the popup paints while the flash is live, and is silent once expired
img, d = blank()
app._output_overlay(d)
assert img != Image.new("RGB", (ui.W, ui.H), (0, 0, 0)), "popup drew nothing"
img, d = blank()
app.output_flash = time.monotonic() - 1  # expired
app._output_overlay(d)
assert img == Image.new("RGB", (ui.W, ui.H), (0, 0, 0)), "expired popup lingered"
print("2. popup paints while live, self-clears when expired OK")

# 3. the reverse flip (local -> bt) names the bluetooth speaker
CUR["output"] = "local"
POSTS.clear()
app._toggle_output()
assert POSTS == [("/output", {"device": "bt"})], POSTS
assert app.output_shown == "Bluetooth speaker", app.output_shown
print("3. reverse flip names the bluetooth speaker OK")

# 4. switching to a device with no sound card carries the warning into
# the popup (the same detail the old message appended)
CUR["output"] = "bt"
WARN["on"] = True
POSTS.clear()
app._toggle_output()
assert app.output_warning is True, "no-sound-card warning dropped"
img, d = blank()
app._output_overlay(d)
assert img != Image.new("RGB", (ui.W, ui.H), (0, 0, 0)), "warning popup empty"
print("4. no-sound-card switch shows the warning popup OK")

# 5. a failed switch (API down) neither arms the popup nor blocks
WARN["on"] = False
app.output_flash = 0.0
app.dirty = False


def boom(path, body=None, timeout=15):
    raise OSError("api down")


ui.api_post = boom
app._toggle_output()
assert app.output_flash == 0.0, "a failed switch must not arm the popup"
assert BLOCKED == [], "a failed switch must not block either"
print("5. a failed switch is a quiet no-op OK")

print("ui_output_popup: all OK")
