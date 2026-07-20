#!/usr/bin/env python3
"""Gate the input layer's B-hold gesture used by the category carousel.
hold-B only ever emitted 'b_long' in now-playing (gesture_mode); the
category carousel's 'step up a level' needs it there too, armed by the
new b_hold flag — WITHOUT changing menus/flat carousel (b_hold off). A
short B still resolves to a plain 'b' (a tile flip) on release."""
import os
import sys
import threading
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")

import ui  # noqa: E402


def fresh():
    inp = object.__new__(ui.GpioInput)
    inp.queue = []
    inp.gesture_mode = False
    inp.b_hold = False
    inp.down = {}
    inp.tainted = set()
    inp._long_sent = {}
    inp._b_gesture = False
    inp.wake = threading.Event()
    return inp


CLOCK = [0.0]
ui.time.monotonic = lambda: CLOCK[0]

# 1. b_hold OFF (menus / flat carousel): holding B past LONG_S emits NO
# b_long — the flat carousel never had an 'up', and mustn't sprout one
inp = fresh()
CLOCK[0] = 0.0
inp._pressed("b")
assert inp._b_gesture is False, "b_hold off must not arm the hold"
CLOCK[0] = inp.LONG_S + 0.2
assert "b_long" not in inp._events()
CLOCK[0] += 0.1
inp._released("b")
assert inp._events() == ["b"], "short/held B stays a plain b when unarmed"
print("1. b_hold off: hold-B never emits b_long (menus/flat unchanged) OK")

# 2. b_hold ON (category carousel): a held B DOES emit b_long, while still
# held (no waiting for release) — that's the 'step up to categories'
inp = fresh()
inp.b_hold = True
CLOCK[0] = 0.0
inp._pressed("b")
assert inp._b_gesture is True, "b_hold must arm the hold detection"
CLOCK[0] = inp.LONG_S + 0.2
assert inp._events() == ["b_long"], "a held B must emit b_long when armed"
# the later release must NOT then also queue a stray 'b'
CLOCK[0] += 0.1
inp._released("b")
assert inp._events() == [], "the b_long release must be swallowed"
print("2. b_hold on: a held B emits b_long, release swallowed OK")

# 3. b_hold ON but a SHORT B (released before LONG_S) is a plain 'b' — a
# tile flip, not an up-navigation
inp = fresh()
inp.b_hold = True
CLOCK[0] = 0.0
inp._pressed("b")
CLOCK[0] = 0.2                 # released well before LONG_S
inp._released("b")
assert inp._events() == ["b"], "a short B must still flip (plain b)"
print("3. b_hold on: a short B still resolves to a tile flip OK")

print("INPUT B-HOLD OK — the category carousel gets a real hold-B on the "
      "buttons, and menus/flat carousel are untouched.")
