#!/usr/bin/env python3
"""Gate message-screen word wrapping (field 2026-07-30: the extras
'no TV found' note ran straight off the 240px panel — draw_message drew
any single-line text as one centered line, however long)."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")

from PIL import Image, ImageDraw  # noqa: E402

import ui  # noqa: E402

d = ImageDraw.Draw(Image.new("RGB", (ui.W, ui.H)))
MAX = ui.W - 16

# 1. every wrapped line fits the panel
msg = "RetroPie: no TV found — connect HDMI and try again"
lines = ui.wrap_text(d, msg, ui.F_MED, MAX)
assert len(lines) >= 2, "a long one-liner must wrap"
for ln in lines:
    assert d.textlength(ln, font=ui.F_MED) <= MAX, f"line overflows: {ln!r}"
assert " ".join(lines) == msg, "no words may be lost or reordered"
print("1. long message wraps, every line fits, nothing lost OK")

# 2. explicit \n stays a hard break (existing messages rely on it)
lines = ui.wrap_text(d, "Starting RetroPie ...\n(first run can take a minute)",
                     ui.F_MED, MAX)
assert lines[0] == "Starting RetroPie ..."
print("2. explicit newlines preserved OK")

# 3. short text is unchanged; degenerate inputs never explode
assert ui.wrap_text(d, "Paired!", ui.F_MED, MAX) == ["Paired!"]
assert ui.wrap_text(d, "", ui.F_MED, MAX) == [""]
assert ui.wrap_text(d, "x" * 400, ui.F_MED, MAX) == ["x" * 400], \
    "an unbreakable monster word stays one line (clipped beats a loop)"
print("3. short/empty/monster-word cases OK")


# 4. draw_message renders the wrapped lines (pixels beyond the margin
#    stay background on both edges)
class FakeDisplay:
    last = None

    def show(self, img):
        FakeDisplay.last = img


app = ui.App.__new__(ui.App)
app.display = FakeDisplay()
app.system = {}
app.draw_message(msg)
frame = FakeDisplay.last
edge_px = [frame.getpixel((x, y)) for x in (0, 1, 2, ui.W - 3, ui.W - 2)
           for y in range(40, ui.H - 40, 4)]
assert all(p == ui.BG for p in edge_px), \
    "wrapped message must not paint into the panel edges"
print("4. draw_message keeps text off the panel edges OK")

print("\nall ui_message_wrap checks passed")
