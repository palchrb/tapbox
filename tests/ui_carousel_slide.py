#!/usr/bin/env python3
"""The carousel shelf slide (playful-ui, design review 2026-08-12).
What must hold:

1. THE GATE: with VIBB_UI_PNG set the animation is force-off, so all
   existing UI tests stay byte- and call-count-stable.
2. THE MASH RULE end to end: an event caught mid-slide aborts the glide
   AND is never lost — it lands in _pending for the main loop.
3. A caught mid-slide acts on the LANDED album (index committed before
   the first frame).
4. Overlays: volume mode never flips (so never slides); a modal BT
   popup skips the animation but still changes the index.
5. The shipped path stays safe: display-off flips draw nothing, dirty
   set by an art landing mid-slide survives, marquee state untouched.
6. The input state machine tolerates the >=50ms poll cadence: no
   phantom holds, each event delivered exactly once.
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
os.environ["VIBB_EMOJI"] = "0"

import ui  # noqa: E402


class FakeDisplay:
    on = True

    def __init__(self):
        self.shows = 0

    def show(self, img):
        self.shows += 1

    def set_backlight(self, on):
        pass

    def set_brightness(self, b):
        pass


class FakeInputs:
    """poll(t) returns the next scripted batch; records every call."""

    def __init__(self, batches=()):
        self.batches = list(batches)
        self.calls = []
        self.b_hold = False
        self.gesture_mode = False

    def poll(self, timeout):
        self.calls.append(timeout)
        return self.batches.pop(0) if self.batches else []


LIB = {"sections": [{"name": "Alt", "entries": [
    {"id": "e0", "name": "Null", "target": "https://x/0"},
    {"id": "e1", "name": "En", "target": "https://x/1"},
    {"id": "e2", "name": "To", "target": "https://x/2"},
]}]}


def make_app(inputs):
    app = ui.App(FakeDisplay(), inputs)
    app._lib_at = time.monotonic() + 999
    app.library = LIB
    app.settings = {"simple_nav": 1}
    app.view = "carousel"
    app.car_sel = 0
    return app


# 1. the gate: UI_ANIM is off under the PNG env, and a flip draws NOTHING
assert ui.UI_ANIM is False, "VIBB_UI_PNG must force the animation off"
app = make_app(FakeInputs())
app.handle_carousel("y")
assert app.car_sel == 1
assert app.display.shows == 0, "gated flip must not call show()"
print("1. PNG gate holds — flip changes index, zero frames OK")

# The remaining pins run with the animation ON via the module flag.
ui.UI_ANIM = True

# 2. a clean slide draws frames and lands; nothing pending
app = make_app(FakeInputs())          # polls always come back empty
t0 = time.monotonic()
app.handle_carousel("y")
took = time.monotonic() - t0
assert app.car_sel == 1
clean_frames = app.display.shows
assert clean_frames >= 2, f"a calm flip should glide ({clean_frames})"
assert app._pending == []
assert took < 0.5, f"glide must respect the wall cap ({took:.3f}s)"
print(f"2. clean flip glides ({clean_frames} frames, "
      f"{took*1000:.0f}ms) and lands OK")

# 3. THE MASH RULE: a press caught mid-slide aborts and is NOT lost
app = make_app(FakeInputs(batches=[[], ["y"]]))
app.handle_carousel("y")
assert app.car_sel == 1               # first press committed
assert app._pending == ["y"], "the caught press must be requeued"
assert app.display.shows < clean_frames, \
    f"aborted slide must cut frames ({app.display.shows} vs {clean_frames})"
# the main loop drains _pending next pass — simulate it:
pend, app._pending = app._pending, []
for ev in pend:
    app.handle_carousel(ev)
assert app.car_sel == 2, "the requeued press must still advance"
print("3. mash rule: abort + requeue, press never lost OK")

# 4. A caught mid-slide plays the LANDED album, never the departing one
app = make_app(FakeInputs(batches=[[], ["a"]]))
played = []
app._play_async = lambda body, entry=None: played.append(body["id"])
app.handle_carousel("y")              # 0 -> 1, "a" caught mid-slide
pend, app._pending = app._pending, []
for ev in pend:
    app.handle_carousel(ev)
assert played == ["e1"], f"A must act on the landed tile: {played}"
print("4. A mid-slide plays the landed album OK")

# 5a. volume mode: B/Y adjust volume — index untouched, zero frames
app = make_app(FakeInputs())
app.vol_mode_until = time.monotonic() + 5
vol = []
app._volume_mode = lambda delta=None: vol.append(delta)
app.handle_carousel("y")
assert app.car_sel == 0 and app.display.shows == 0 and vol == [5]
print("5a. volume mode flips nothing, slides nothing OK")

# 5b. modal BT popup: index changes, animation is skipped
app = make_app(FakeInputs())
app.status = {"bt_lost": True}
app.handle_carousel("y")
assert app.car_sel == 1
assert app.display.shows == 0, "a modal popup must not vanish mid-slide"
print("5b. BT popup present: flip yes, slide no OK")

# 5c. display off: no frames (wake handling lives in run(), not here)
app = make_app(FakeInputs())
app.display.on = False
app.handle_carousel("y")
assert app.car_sel == 1 and app.display.shows == 0
print("5c. dark screen never animates OK")

# 5e. the label never blinks away: the LANDING album's name is in the
#     very first slide frame (baked into the base), byte-identical to
#     what a from-scratch base with that label would show in the band
class GrabDisplay(FakeDisplay):
    def __init__(self):
        super().__init__()
        self.frames = []

    def show(self, img):
        super().show(img)
        self.frames.append(img.copy())


app = make_app(FakeInputs())
app.display = GrabDisplay()
app.handle_carousel("y")            # lands on "En"
assert app.display.frames, "clean flip must have drawn frames"
BAND = (0, 200, ui.W, 226)
first = app.display.frames[0].crop(BAND)
assert first.getbbox() is not None, \
    "label band must not be blank during the glide"
want = ui.Image.new("RGB", (ui.W, ui.H), ui.BG)
d = ui._draw(want)
win, _ = ui.marquee("En", 20, t0=time.monotonic())
d.text((ui.W // 2, 206), win, font=ui.F_MED, fill=ui.FG, anchor="ma")
assert first.tobytes() == want.crop(BAND).tobytes(), \
    "the band must show the landing album's name, nothing else"
print("5e. landing label visible from frame 1 — no blink OK")

# 5d. dirty from an art landing mid-slide survives; marquee untouched
app = make_app(FakeInputs())
app.dirty = True
before = getattr(app, "marquee_active", None)
app.handle_carousel("y")
assert app.dirty is True, "the slide must never clear dirty"
assert getattr(app, "marquee_active", None) == before
print("5d. dirty and marquee state survive the slide OK")

# 6. input machinery at the 50ms poll cadence: real LgpioInput._sample
#    driven by a scripted clock+levels — one event per press, no
#    phantom hold left behind (the 25ms hazard from the design review)
lg_levels = {"now": 0.0, "timeline": []}  # (time, {pin: level})


class FakeLG:
    SET_PULL_UP = 0

    @staticmethod
    def gpio_read(h, pin):
        lvl = 1  # pull-up idle
        for t, levels in lg_levels["timeline"]:
            if lg_levels["now"] >= t and pin in levels:
                lvl = levels[pin]
        return lvl


real_mono = time.monotonic
try:
    inp = object.__new__(ui.LgpioInput)
    inp._lg = FakeLG
    inp._h = 1
    inp.queue = []
    inp.down = {}
    inp.tainted = set()
    inp._long_sent = {}
    inp._b_gesture = False
    inp._a_gesture = False
    inp.wake = ui.threading.Event()
    inp._edge_at = {n: 0.0 for n in ui.GpioInput.PINS}
    inp._level = {n: 1 for n in ui.GpioInput.PINS}  # pull-up idle
    inp.b_hold = False
    inp.gesture_mode = False
    ui.time.monotonic = lambda: lg_levels["now"]
    Y = ui.GpioInput.PINS["y"] if hasattr(ui.GpioInput, "PINS") else 24
    # a 90ms press: down at t=0.01, up at t=0.10 (levels active-low)
    lg_levels["timeline"] = [(0.01, {Y: 0}), (0.10, {Y: 1})]
    got = []
    for t in (0.0, 0.05, 0.10, 0.15, 0.20):
        lg_levels["now"] = t
        got += inp.poll(0)
    assert got == ["y"], f"one press must yield exactly one event: {got}"
    assert "y" not in inp.down, "no phantom hold may remain"
finally:
    ui.time.monotonic = real_mono
print("6. 50ms-cadence polling: one event, no phantom hold OK")

print("\nCAROUSEL SLIDE OK — glides when it can, aborts when the "
      "finger is faster, and never lags or loses a press.")
