#!/usr/bin/env python3
"""Gate the Pirate Audio button state machine after the A/B role swap:
A = select (menus) / play-pause (now), B = back (menus) / previous (now,
hold = back to the episode list). The hold gesture lives on B now — a
B-release after the long fired must NOT leak a 'previous' or menu press."""
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")  # no SPI in tests

import ui  # noqa: E402


def fresh(gesture):
    inp = object.__new__(ui.GpioInput)  # skip __init__ (needs gpiozero)
    inp.queue = []
    inp.gesture_mode = gesture
    inp.down = {}
    inp.tainted = set()
    inp._b_long_sent = False
    inp._b_gesture = False
    inp._x_long_sent = False
    inp.wake = threading.Event()
    return inp


# 1. menus: A press+release -> 'a' (select), B press+release -> 'b' (back)
inp = fresh(gesture=False)
inp._pressed("a"), inp._released("a")
inp._pressed("b"), inp._released("b")
assert inp._events() == ["a", "b"], inp.queue
print("1. menu presses deliver a (select) and b (back) OK")

# 2. now-playing: a quick B tap is 'previous' (fires on release)
inp = fresh(gesture=True)
inp._pressed("b")
assert inp._events() == [], "b must not fire while still held"
inp._released("b")
assert inp._events() == ["b"]
print("2. quick B in now-playing -> previous OK")

# 3. now-playing: B held past LONG_S fires 'b_long' while still held,
# and the eventual release adds NOTHING (no phantom previous/menu press)
inp = fresh(gesture=True)
inp._pressed("b")
inp.down["b"] = time.monotonic() - (ui.GpioInput.LONG_S + 0.1)
assert inp._events() == ["b_long"]
inp.gesture_mode = False  # b_long navigated away from now-playing
inp._released("b")
assert inp._events() == [], "release after b_long must be swallowed"
print("3. held B -> b_long once, release swallowed OK")

# 4. A+B held HOLD_S -> settings, both releases swallowed
inp = fresh(gesture=False)
inp._pressed("a"), inp._pressed("b")
t = time.monotonic() - (ui.GpioInput.HOLD_S + 0.1)
inp.down["a"] = inp.down["b"] = t
assert inp._events() == ["settings"]
inp._released("a"), inp._released("b")
assert inp._events() == [], "combo releases must not act"
print("4. A+B combo -> settings, no leakage OK")

# 5. overlapping A+B released early = failed combo, both swallowed
inp = fresh(gesture=False)
inp._pressed("a"), inp._pressed("b")
inp._released("a")
inp._released("b")
assert inp._events() == [], "failed combo must not select/back"
print("5. failed combo swallowed OK")

print("UI BUTTONS OK — A selects/plays, B backs/rewinds, hold-B goes back.")
