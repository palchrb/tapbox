#!/usr/bin/env python3
"""The ST7789 fast push. Rig 2026-08-12: the frame log said
"compose 1ms + push 75ms per frame" — the Pimoroni library's
display() converts every frame into a 115200-entry Python list
(.tolist()) before chunking it to SPI, ~60ms of pure interpreter
work per frame at 600MHz. ui._rgb565 does the identical conversion
straight to bytes. Pins: byte-for-byte layout (565 packing, big
endian, rot90 orientation) against an independent pure-Python
reference, and the forever-fallback when the library surprises us."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
os.environ["TAPBOX_EMOJI"] = "0"

import ui  # noqa: E402
from PIL import Image  # noqa: E402


def reference(img, rotation):
    """The Pimoroni image_to_data layout, written independently:
    np.rot90(a, k) means out[r][c] = in[c][H-1-r] per quarter turn,
    then RGB888 -> 565 (5/6/5 truncation), high byte on the wire
    first. Pure Python, no numpy — the point is a second opinion."""
    px = img.convert("RGB").load()
    w, h = img.size
    out = bytearray()
    for _ in range(rotation // 90):
        w, h = h, w
    # only need the k=1 case the display uses (240x240 stays square)
    assert rotation == 90 and img.size[0] == img.size[1]
    n = img.size[0]
    for r in range(n):
        for c in range(n):
            R, G, B = px[n - 1 - r, c][:3][0], px[n - 1 - r, c][1], \
                px[n - 1 - r, c][2]
            v = ((R & 0xF8) << 8) | ((G & 0xFC) << 3) | (B >> 3)
            out += bytes(((v >> 8) & 0xFF, v & 0xFF))
    return bytes(out)


# 1. primary colors land in the right 565 slots (and full white/black)
img = Image.new("RGB", (240, 240), (0, 0, 0))
img.putpixel((0, 0), (255, 0, 0))
buf = ui._rgb565(img, 90)
assert len(buf) == 240 * 240 * 2
assert ui._rgb565(Image.new("RGB", (240, 240), (255, 0, 0)), 90)[:2] \
    == bytes((0xF8, 0x00))
assert ui._rgb565(Image.new("RGB", (240, 240), (0, 255, 0)), 90)[:2] \
    == bytes((0x07, 0xE0))
assert ui._rgb565(Image.new("RGB", (240, 240), (0, 0, 255)), 90)[:2] \
    == bytes((0x00, 0x1F))
assert ui._rgb565(Image.new("RGB", (240, 240), (255, 255, 255)), 90)[:2] \
    == bytes((0xFF, 0xFF))
print("1. 565 packing and byte order OK")

# 2. byte-for-byte against the independent reference on a busy image
#    (gradient + a few landmarks so rotation errors cannot cancel out)
img = Image.new("RGB", (240, 240))
for y in range(240):
    for x in range(240):
        img.putpixel((x, y), (x % 256, y % 256, (x * y) % 256))
img.putpixel((3, 7), (255, 0, 0))
img.putpixel((239, 0), (0, 255, 0))
assert ui._rgb565(img, 90) == reference(img, 90), \
    "fast conversion must match the library layout byte-for-byte"
print("2. full-frame equivalence with the reference conversion OK")

# 3. the happy path: set_window, DC flipped to data via the library's
#    own data(b""), then the WHOLE frame in one writebytes2 — the
#    exact _rgb565 bytes, no chunk loop, nothing read back
class FakeSpi:
    def __init__(self):
        self.written = []

    def writebytes2(self, buf):
        self.written.append(bytes(buf))


class FakeDisp:
    def __init__(self, spi=None):
        self.windows = 0
        self.dc_data = 0
        self.displayed = []
        if spi is not None:
            self._spi = spi

    def set_window(self):
        self.windows += 1

    def data(self, buf):
        assert buf == b"", "fast path must not push pixels via data()"
        self.dc_data += 1

    def display(self, im):
        self.displayed.append(im)


spi = FakeSpi()
d = object.__new__(ui.St7789Display)
d.disp = FakeDisp(spi)
d._fast = True
im = Image.new("RGB", (240, 240), (10, 20, 30))
d.show(im)
assert d.disp.windows == 1 and d.disp.dc_data == 1
assert spi.written == [ui._rgb565(im, 90)], \
    "the frame on the wire must be the _rgb565 bytes, in one write"
assert d.disp.displayed == []
print("3. happy path: one writebytes2 with the exact frame bytes OK")

# 4. the library surprising us (no _spi, spidev without writebytes2,
#    anything) switches to stock display() — permanently, one log line
d = object.__new__(ui.St7789Display)
d.disp = FakeDisp()                      # no _spi attribute at all
d._fast = True
d.show(im)
assert d.disp.displayed == [im], "failed fast path must still paint"
assert d._fast is False
d.show(im)
assert d.disp.windows == 1, "fallback must be permanent, not per-frame"
assert len(d.disp.displayed) == 2
print("4. fallback paints the frame and sticks OK")

# 5. TAPBOX_FAST_PUSH=0 is the kill switch (mirrors _fast init)
d2 = object.__new__(ui.St7789Display)
d2.disp = FakeDisp(FakeSpi())
d2._fast = False           # what __init__ does under TAPBOX_FAST_PUSH=0
d2.show(im)
assert d2.disp.windows == 0 and len(d2.disp.displayed) == 1
print("5. kill switch goes straight to library display() OK")

print("\nFAST PUSH OK — same bytes on the wire, no 115200-entry list.")
