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
    inp._long_sent = {}
    inp._b_gesture = False
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

# 6. now-playing: quick Y = next (on release); held Y = the episode
# picker, exactly once, release swallowed. In menus Y stays instant.
inp = fresh(gesture=True)
inp._pressed("y")
assert inp._events() == [], "y must not fire while held (gesture mode)"
inp._released("y")
assert inp._events() == ["y"]
inp._pressed("y")
inp.down["y"] = time.monotonic() - (ui.GpioInput.LONG_S + 0.1)
assert inp._events() == ["y_long"]
inp._released("y")
assert inp._events() == [], "release after y_long must be swallowed"
inp = fresh(gesture=False)
inp._pressed("y")
assert inp._events() == ["y"], "menu Y must stay an instant press"
print("6. Y: quick=next, hold=episode picker, instant in menus OK")

# --- the dark-screen waking press ------------------------------------------
# The press that wakes a dark screen is swallowed — EXCEPT A while music
# plays: needing a second press to pause read as "won't let me pause"
# (field 2026-07-18 20:18). Playing is checked with a FRESH probe:
# self.status can be hours stale in the dark, and a stale playing=True
# would replay the last target — surprise audio from a bag.

app = object.__new__(ui.App)
app.status = {"playing": True}  # STALE — must never be trusted
app.last_status = 1e18
POSTS = []
PROBE = [{"playing": True}]


def fake_get(path, timeout=10):
    if isinstance(PROBE[0], Exception):
        raise PROBE[0]
    return PROBE[0]


ui.api_get = fake_get
ui.api_post = lambda path, body=None, timeout=15: POSTS.append(path)

# 7. dark A while a fresh probe confirms playing -> pause fires with the
# wake (one press, not two)
app._wake_press(["a"])
assert POSTS == ["/playpause"], POSTS
print("7. dark A while playing: pause fires on the waking press OK")

# 8. fresh probe says idle -> wake only, NEVER start playback blind
POSTS.clear()
PROBE[0] = {"playing": False}
app._wake_press(["a"])
assert POSTS == [], f"dark A while idle must not start audio: {POSTS}"
print("8. dark A while idle: wake only, no surprise audio OK")

# 9. probe unreachable -> wake only (stale status must not decide)
PROBE[0] = OSError("daemon busy")
app._wake_press(["a"])
assert POSTS == [], "an unreachable probe must fail to plain wake"
print("9. dark A with no probe: stale status never trusted OK")

# 10. B/Y/X stay wake-only even while playing — buttons squeezed in a
# bag must not scramble the queue in the dark
PROBE[0] = {"playing": True}
app._wake_press(["b"])
app._wake_press(["y"])
app._wake_press(["x"])
assert POSTS == [], f"only A may act from the dark: {POSTS}"
print("10. dark B/Y/X: wake only (no queue scrambling from a bag) OK")

print("UI BUTTONS OK — A selects/plays, B backs/rewinds, hold-B goes back, "
      "and a dark A pauses in one press without surprise audio.")
