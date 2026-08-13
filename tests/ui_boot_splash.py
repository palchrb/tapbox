#!/usr/bin/env python3
"""The breathing boot mark, and the handoff that must not race.

The splash is drawn straight from the 240x240 artwork's own numbers —
the mark group at (120,96) scaled 1.9, the wordmark centred on baseline
196 — so the screen IS the artboard and nothing is re-fitted. It
animates on its own thread, filling dead time that already existed
(lgpio init, the library fetch, the first /status) rather than adding
any.

The hazard worth a test is the handoff: two threads owning one SPI
panel. run() must stop AND join the animation before it paints, or a
late splash frame lands on top of the real UI."""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
os.environ["VIBB_EMOJI"] = "0"
os.environ["VIBB_SPLASH_FPS"] = "60"      # keep the test quick

import ui  # noqa: E402

# 1. the rings come from the ARTWORK FILE, and they are not circles:
#    the radius wanders ~4% around the turn. Drawing them as ellipses
#    flattened exactly the hand-shaped character the mark has.
import math  # noqa: E402

assert os.path.exists(ui.MARK_SVG), ui.MARK_SVG
rings = ui.mark_rings()
assert len(rings) == 4, f"four rings, got {len(rings)}"
for pts in rings:
    assert len(pts) > 100, "a ring is a dense polyline, not a few corners"
    rad = [math.hypot(x, y) for x, y in pts]
    assert (max(rad) - min(rad)) / max(rad) > 0.02, \
        "these rings are organic — a perfect circle means we lost them"
# biggest first, so the phase stagger runs outward
assert [max(math.hypot(x, y) for x, y in p) for p in rings] == \
    sorted([max(math.hypot(x, y) for x, y in p) for p in rings], reverse=True)
assert (ui.MARK_S, ui.MARK_CX, ui.MARK_CY) == (1.9, 120.0, 96.0)
assert (ui.MARK_WORD_X, ui.MARK_WORD_Y) == (120.0, 196.0)
assert ui.MARK_WORD_SIZE == 52 and ui.MARK_WORD_TRACK == -1.56
# and it all fits: the outer ring at full breath must not clip
assert ui.MARK_CY - 25.5 * ui.MARK_S * 1.04 > 0 and ui.MARK_WORD_Y < ui.H
print(f"1. {len(rings)} organic rings read from the artwork, and they fit OK")

# 1b. an unreadable artwork must not stop a boot — plain rings instead
saved, ui._RINGS[:] = list(ui._RINGS), []
real_svg, ui.MARK_SVG = ui.MARK_SVG, "/nonexistent/nope.svg"
try:
    fb = ui.mark_rings()
    assert len(fb) == 4 and len(fb[0]) > 100
finally:
    ui.MARK_SVG = real_svg
    ui._RINGS[:] = saved
print("1b. a missing artwork falls back to plain rings, never breaks OK")

# 1c. the wordmark uses the artwork's own face. Nunito ships VARIABLE,
#     so the Black axis must be selected — the default instance is
#     ExtraLight, the opposite of the mark.
name = ui._mark_font().getname()
if os.path.exists(os.path.join(os.path.dirname(ui.MARK_SVG), "Nunito.ttf")):
    assert name == ("Nunito", "Black"), f"got {name} — hairline wordmark"
print(f"1c. wordmark face: {name[0]} {name[1]} OK")

# 2. the breathe: symmetric, bounded, and STAGGERED across the rings —
#    the stagger is what makes the breath travel outward
lo, hi = 0.94, 1.04
vals = [ui._breathe(t / 20.0) for t in range(int(ui.BREATHE_S * 20))]
assert min(vals) >= lo - 1e-9 and max(vals) <= hi + 1e-9
assert abs(ui._breathe(0.0) - lo) < 1e-6, "starts at the bottom of the breath"
assert abs(ui._breathe(ui.BREATHE_S / 2) - hi) < 1e-6, "peaks halfway"
assert abs(ui._breathe(0.7) - ui._breathe(0.7 + ui.BREATHE_S)) < 1e-9, "loops"
assert ui._breathe(1.0) != ui._breathe(1.0, phase=-0.28), "rings differ"
print("2. breathe is bounded, symmetric, looping and staggered OK")

# 3. opacity is blended against the KNOWN background, so a stroke at
#    full opacity is the ring colour and at zero is invisible
assert ui._blend(ui.MARK_RING, ui.BG, 1.0) == ui.MARK_RING
assert ui._blend(ui.MARK_RING, ui.BG, 0.0) == ui.BG
print("3. opacity blends exactly against the background OK")

# 4. a frame is a full-screen image, and it CHANGES over the breath
a = ui.splash_frame(0.0)
b = ui.splash_frame(ui.BREATHE_S / 2)
assert a.size == (ui.W, ui.H) and b.size == (ui.W, ui.H)
assert a.tobytes() != b.tobytes(), "the mark must actually move"
assert a.getpixel((5, 5)) == ui.BG, "corners stay background"
print("4. frames are full-screen and animate OK")


# 4b. the wordmark is CENTRED even with negative tracking — drawing
#     glyph by glyph is easy to get subtly off-centre
from PIL import Image as _I, ImageDraw as _D  # noqa: E402

probe = _I.new("RGB", (ui.W, ui.H), ui.BG)
ui._tracked_text(_D.Draw(probe), (ui.MARK_WORD_X, ui.MARK_WORD_Y),
                 "vibb", ui._mark_font(), ui.MARK_WORD, ui.MARK_WORD_TRACK)
box = probe.getbbox()
assert box, "the wordmark must actually draw"
assert abs((box[0] + box[2]) / 2 - ui.MARK_WORD_X) <= 2, \
    f"wordmark off-centre: {box}"
print("4b. tracked wordmark stays centred OK")

# 5. THE HANDOFF: stop() must join, so no frame can land after it returns
class CountingDisplay:
    def __init__(self):
        self.shows = 0
        self.lock = threading.Lock()

    def show(self, img):
        with self.lock:
            self.shows += 1

    def set_backlight(self, on):
        pass

    def set_brightness(self, b):
        pass


disp = CountingDisplay()
stop = ui._boot_splash(disp)
time.sleep(0.4)
assert disp.shows > 1, "the mark must be animating, not one static frame"
stop()
after = disp.shows
time.sleep(0.3)
assert disp.shows == after, \
    "a frame landed AFTER stop() — the splash and the UI can both own " \
    "the panel"
print(f"5. stop() joins the thread; no frame after it ({after} drawn) OK")

# 6. a display that explodes must not take the boot down with it
class BrokenDisplay(CountingDisplay):
    def show(self, img):
        raise RuntimeError("panel on fire")


stop = ui._boot_splash(BrokenDisplay())
time.sleep(0.2)
stop()          # must not raise
print("6. a broken panel skips the splash, never blocks boot OK")

# 7. run() actually calls it — a stop that is never invoked is the bug
src = open(ui.__file__, encoding="utf-8").read()
i_land = src.index("self._boot_landing()")
i_ready = src.index('log("ready")', i_land)
assert "splash_done" in src[i_land:i_ready], \
    "run() must stop the splash between landing and ready"
assert "app.splash_done = splash_done" in src
print("7. run() stops the splash before it paints for real OK")

print("\nBOOT SPLASH OK — the logo breathes, and lets go of the panel "
      "before the UI needs it.")
