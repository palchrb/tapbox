#!/usr/bin/env python3
"""The breathing boot mark, and the handoff that must not race.

The splash is drawn from the artwork's own 186x84 coordinates scaled to
full screen width, so the logo is the logo — not a re-composed version
of it. It animates on its own thread, filling dead time that already
existed (lgpio init, the library fetch, the first /status) rather than
adding any.

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

# 1. the geometry is the ARTWORK's, scaled — not eyeballed
assert abs(ui.MARK_S - ui.W / 186.0) < 1e-9, "full width, height follows"
assert abs(ui.MARK_CX - 42.0 * ui.MARK_S) < 0.01
assert abs(ui.MARK_CY - ui.H / 2.0) < 0.5, \
    "the mark is centred in the source, so it lands on the screen's centre"
assert abs(ui.MARK_TOP - (ui.H - 84.0 * ui.MARK_S) / 2) < 0.01, "letterbox"
assert ui.MARK_RADII[0] > ui.MARK_RADII[-1] and len(ui.MARK_RADII) == 4
print("1. geometry derives from the source coordinates OK")

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
assert a.getpixel((5, 5)) == ui.BG, "letterbox stays background"
print("4. frames are full-screen and animate OK")


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
