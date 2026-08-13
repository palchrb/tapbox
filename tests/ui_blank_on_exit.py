#!/usr/bin/env python3
"""Gate that vibb-ui leaves the panel DARK when it stops.

The ST7789 holds its last frame indefinitely and the backlight is ours
to drive (BCM13 via PWM), so simply exiting left a frozen Vibb menu
lit on the box — field 2026-08-04: it sat there through a whole
RetroPie session, wasting power and looking broken. Every stop reason
(extras handoff, service restart, shutdown) arrives as SIGTERM, so the
blanking belongs in the UI, not in each extra."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402


class FakeDisplay:
    def __init__(self):
        self.calls = []
        self.last = None

    def set_backlight(self, on):
        self.calls.append(("backlight", on))

    def show(self, img):
        self.calls.append(("show", img))
        self.last = img


# 1. backlight OFF first, then black pixels — order matters: a panel
#    lit again later must not flash the stale frame
d = FakeDisplay()
ui.blank_screen(d)
assert [c[0] for c in d.calls] == ["backlight", "show"], d.calls
assert d.calls[0][1] is False, "backlight must be turned off"
assert d.last.size == (ui.W, ui.H)
assert d.last.getpixel((0, 0)) == (0, 0, 0), "panel must be blacked out"
assert d.last.getpixel((ui.W // 2, ui.H // 2)) == (0, 0, 0)
print("1. blank_screen: backlight off, then an all-black frame OK")


# 2. a display that throws (SPI gone, GPIO busy) must not stop the exit
class BrokenDisplay:
    def set_backlight(self, on):
        raise OSError("spi gone")

    def show(self, img):
        raise OSError("spi gone")


ui.blank_screen(BrokenDisplay())  # must not raise
print("2. a broken display cannot block shutdown OK")

# 3. the wiring: SIGTERM handled, and the run loop blanks on the way out
#    whatever ends it
import inspect  # noqa: E402
src = inspect.getsource(ui.main)
assert "signal.signal(signal.SIGTERM" in src, \
    "SIGTERM must be handled — systemd stops us that way"
assert "blank_screen(display)" in src
assert "finally:" in src, "a normal/crashing exit must blank too"
print("3. SIGTERM handler + finally-blank wired in main() OK")

print("\nall ui_blank_on_exit checks passed")
