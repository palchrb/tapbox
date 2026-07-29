#!/usr/bin/env python3
"""Gate the charging bolt in the screen's battery gauge.

Before 2026-07-29 'plugged' only changed the gauge COLOR — which is
green above 20% anyway, so a charging 50% was indistinguishable from an
idle 50% (the low-battery case was the only visible one). The bolt must
render over BOTH the filled and the empty half (an early version drew
it in BG and drowned in the empty side), and never appear unplugged."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")

from PIL import Image, ImageDraw  # noqa: E402

import ui  # noqa: E402

X, Y, W_, H = ui.W - 32, 8, 24, 11


def render(plugged, pct):
    img = Image.new("RGB", (ui.W, 40), (0, 60, 0))  # non-BG canvas
    ui._BATT_COLOR[0] = ui.GOOD  # reset the color hysteresis between runs
    ui.battery_corner(ImageDraw.Draw(img),
                      {"battery": pct, "plugged": plugged,
                       "wifi": {"enabled": False}})
    return img


def bolt_px(img):
    return sum(1 for px in range(X, X + W_ + 1)
               for py in range(Y, Y + H + 1)
               if img.getpixel((px, py)) == ui.HILITE)


# 1. plugged: the bolt is visible at every fill level — including 50%,
#    where half of it sits over the EMPTY (dark) part of the gauge
for pct in (5, 50, 95):
    assert bolt_px(render(True, pct)) >= 6, f"bolt must show at {pct}%"
print("1. bolt visible at 5/50/95% while charging OK")

# 2. unplugged at healthy levels: no HILITE anywhere in the gauge
#    (below 20% the gauge itself goes orange — excluded on purpose)
for pct in (30, 50, 95):
    assert bolt_px(render(False, pct)) == 0, f"no bolt unplugged at {pct}%"
print("2. no bolt when not charging OK")

# 3. plugged forces the gauge green even at low percent (pre-existing
#    contract the bolt builds on) — and the bolt shows there too
img = render(True, 8)
w, h = img.size
assert any(img.getpixel((px, py)) == ui.GOOD
           for px in range(X, X + W_) for py in range(Y, Y + H)), \
    "plugged gauge must be green"
assert bolt_px(img) >= 6
print("3. low battery + charger: green gauge + bolt OK")

print("\nall ui_battery_bolt checks passed")
