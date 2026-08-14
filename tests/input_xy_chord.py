#!/usr/bin/env python3
"""Gate the X+Y extras chord in the input layer (mirror of hold-A+B).

The dangerous part of adding the combo was the side effects on plain
X/Y: they moved from press-fired to RELEASE-fired in menus (so a chord
finger can't navigate the menu), and the now-playing X/Y holds must
neither fire in menus (X/Y are now tracked there too) nor while a
combo is forming. Each of those is a pinned regression here."""
import os
import sys
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")

import ui  # noqa: E402


def fresh(gesture=False):
    inp = object.__new__(ui.GpioInput)
    inp.queue = []
    inp.gesture_mode = gesture
    inp.b_hold = False
    inp.down = {}
    inp.tainted = set()
    inp._long_sent = {}
    inp._b_gesture = False
    inp._a_gesture = False
    inp.wake = threading.Event()
    return inp


CLOCK = [0.0]
ui.time.monotonic = lambda: CLOCK[0]

# 1. held X+Y past HOLD_S -> one 'extras' event, both releases swallowed
inp = fresh()
inp._pressed("x")
CLOCK[0] = 0.1
inp._pressed("y")
CLOCK[0] = 0.1 + inp.HOLD_S + 0.05
assert inp._events() == ["extras"]
inp._released("x")
inp._released("y")
assert inp._events() == [], "combo releases must be swallowed"
print("1. X+Y hold fires extras once, releases swallowed OK")

# 2. a failed combo attempt (released early) delivers NOTHING — not two
#    menu presses
inp = fresh()
inp._pressed("x")
CLOCK[0] += 0.2
inp._pressed("y")
CLOCK[0] += 0.3  # well under HOLD_S
inp._released("x")   # partner still down -> taints y
inp._released("y")
assert inp._events() == [], "failed chord must not navigate the menu"
print("2. early-released chord swallowed OK")

# 3. plain X in a MENU still arrives (now on release), and holding a
#    single X in a menu never fires x_long (that gesture is
#    now-playing-only)
inp = fresh(gesture=False)
inp._pressed("x")
CLOCK[0] += inp.LONG_S + 0.4  # long past LONG_S, well under HOLD_S
assert inp._events() == [], "no x_long outside now-playing"
inp._released("x")
assert inp._events() == ["x"], "menu X fires as a plain press on release"
print("3. menu X: release-fired, no phantom x_long OK")

# 4. now-playing: single-held X still fires x_long exactly as before...
inp = fresh(gesture=True)
inp._pressed("x")
CLOCK[0] += inp.LONG_S + 0.05
assert inp._events() == ["x_long"]
inp._released("x")
assert inp._events() == [], "release after a fired hold stays silent"
print("4. now-playing x_long unchanged OK")

# 5. ...but never while the combo is forming
inp = fresh(gesture=True)
inp._pressed("x")
CLOCK[0] += 0.2
inp._pressed("y")
CLOCK[0] += inp.LONG_S + 0.1  # past LONG_S for both, under HOLD_S
assert inp._events() == [], "no individual holds while X+Y forms"
CLOCK[0] += inp.HOLD_S
assert inp._events() == ["extras"]
print("5. forming combo suppresses x_long/y_long, then fires extras OK")

# 6. the A+B settings combo is untouched by all of this
inp = fresh()
inp._pressed("a")
inp._pressed("b")
CLOCK[0] += inp.HOLD_S + 0.05
assert inp._events() == ["settings"]
print("6. A+B settings combo unchanged OK")

print("\nall input_xy_chord checks passed")
