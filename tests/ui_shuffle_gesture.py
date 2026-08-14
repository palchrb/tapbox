#!/usr/bin/env python3
"""Hold play in now-playing to shuffle, with the state in the top bar.

Shuffle already existed in the daemon and the PWA; the screen had no
way to reach it and no way to show it. Holding A is the gesture, and a
glyph beside the battery is the whole feedback — neither mpv nor
go-librespot interrupts the current track to reorder what follows, so
there is no gap to announce. Absence of the glyph IS the off state.

The load-bearing pin is #2. A is half of the A+B settings combo, and
the only reason a hold on it is safe is WHERE its branch sits: the
combo branch catches 'both down' first, so reaching the A branch at all
proves B is up. B has worked this way all along; this test is what
stops someone reordering that chain later."""
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

import ui  # noqa: E402
from PIL import Image  # noqa: E402


def mk(now_view=True):
    """A hand-built input machine, mirroring GpioInput.__init__."""
    inp = object.__new__(ui.GpioInput)
    inp.queue = []
    inp.down = {}
    inp.tainted = set()
    inp._long_sent = {}
    inp._b_gesture = False
    inp._a_gesture = False
    inp.b_hold = False
    inp.gesture_mode = now_view
    inp.wake = threading.Event()
    return inp


T = [1000.0]
real_mono = time.monotonic
ui.time.monotonic = lambda: T[0]
try:
    # 1. holding A past LONG_S fires a_long, and the release does NOT
    #    then also deliver a plain "a" — one press, one action
    inp = mk()
    inp._pressed("a")
    T[0] += ui.GpioInput.LONG_S - 0.05
    assert inp._events() == [], "must not fire before the threshold"
    T[0] += 0.1
    assert inp._events() == ["a_long"], "held A must shuffle"
    inp._released("a")
    assert inp._events() == [], "the plain press must be swallowed"
    print("1. held A fires a_long once, release adds nothing OK")

    # 2. THE ONE THAT MATTERS: A+B for settings must never shuffle on
    #    the way. Holding both blocks every long-press, because the
    #    combo branch owns the 'both down' case.
    inp = mk()
    inp._pressed("a")
    T[0] += 0.3
    inp._pressed("b")                      # second finger lands late
    for _ in range(6):                     # well past LONG_S, under HOLD_S
        T[0] += 0.2
        assert inp._events() == [], "no long-press while the combo forms"
    T[0] += ui.GpioInput.HOLD_S
    assert inp._events() == ["settings"], "the combo must still work"
    print("2. A+B opens settings and never toggles shuffle en route OK")

    # 3. a quick tap is still play/pause
    inp = mk()
    inp._pressed("a")
    T[0] += 0.2
    inp._released("a")
    assert inp._events() == ["a"], "a short press is still playpause"
    print("3. a short A is unchanged OK")

    # 4. outside now-playing there is no hold on A at all — in the menus
    #    A selects, and holding it must not invent a gesture
    inp = mk(now_view=False)
    inp._pressed("a")
    T[0] += ui.GpioInput.LONG_S + 0.5
    assert inp._events() == [], "menus have no a_long"
    inp._released("a")
    assert inp._events() == ["a"], "and the press still selects"
    print("4. no a_long outside now-playing OK")
finally:
    ui.time.monotonic = real_mono

# 5. the handler posts the INVERTED state and flips the glyph at once,
#    without waiting for the poller
posted = []
ui.api_post = lambda p, b=None, timeout=None: posted.append((p, b)) or {
    "routed": "mpv", "shuffle": b["enabled"]}


class FakeDisplay:
    on = True

    def show(self, img):
        pass

    def set_backlight(self, on):
        pass

    def set_brightness(self, b):
        pass


class FakeInputs:
    gesture_mode = False
    b_hold = False

    def poll(self, timeout):
        return []


app = ui.App(FakeDisplay(), FakeInputs())
app.view = "now"
app.status = {"title": "Noe", "playing": True, "shuffle": False}
app.system = {"battery": 50}
app.handle_now("a_long")
for _ in range(50):
    if posted:
        break
    time.sleep(0.05)
assert posted == [("/shuffle", {"enabled": True})], posted
assert app.system.get("shuffle") is True, "the glyph must flip immediately"
assert app.status.get("shuffle") is True
app.handle_now("a_long")            # again -> back off
for _ in range(50):
    if len(posted) > 1:
        break
    time.sleep(0.05)
assert posted[1] == ("/shuffle", {"enabled": False}), posted
print("5. a_long posts the inverted state and flips the glyph at once OK")

# 6. nothing routed (sonos, or no session) parks a refusal for the
#    render thread — a silent no-op reads as a broken button
posted.clear()
ui.api_post = lambda p, b=None, timeout=None: {"routed": None,
                                               "shuffle": None}
app.shuffle_refused = False
app.handle_now("a_long")
for _ in range(60):
    if app.shuffle_refused:
        break
    time.sleep(0.05)
assert app.shuffle_refused is True, "a refused shuffle must say so"
print("6. an unroutable shuffle parks a message for the main loop OK")

# 7. the glyph is drawn only when shuffle is on, and the top bar is
#    otherwise byte-identical — absence IS the off state
base = {"battery": 60, "plugged": False, "wifi": {"ip": "1.2.3.4"},
        "bt_ready": True}


def bar(shuffle):
    img = Image.new("RGB", (ui.W, 26), ui.BG)
    ui.battery_corner(ui._draw(img), {**base, "shuffle": shuffle})
    return img


off, on = bar(False), bar(True)
assert off.tobytes() != on.tobytes(), "shuffle-on must look different"
assert bar(False).tobytes() == off.tobytes(), "off must be stable"
# and the difference sits LEFT of the wifi/bt icons, not on top of them
diff = Image.new("RGB", off.size)
diff.paste(on, (0, 0))
box = [i for i in range(ui.W)
       if off.crop((i, 0, i + 1, 26)).tobytes()
       != on.crop((i, 0, i + 1, 26)).tobytes()]
assert box and max(box) < ui.W - 60, \
    f"the glyph must sit left of the existing icons, changed at {box}"
print("7. the glyph appears only when on, left of the other icons OK")

# 8. the daemon's shuffle state reaches self.system through the same
#    fold bt_connected uses — the icon row reads self.system only
app.system = {"battery": 50}
app._set("status", {"bt_connected": True, "shuffle": True, "playing": True})
assert app.system.get("shuffle") is True
assert app.system.get("bt_ready") is True, "must not break the bt fold"
app._set("status", {"bt_connected": True, "shuffle": False, "playing": True})
assert app.system.get("shuffle") is False
print("8. /status shuffle folds into system beside bt_ready OK")

# 9. the daemon refuses on a sonos renderer instead of quietly aiming
#    the command at go-librespot in another room
import daemon  # noqa: E402

sent = []
daemon.go = lambda *a, **k: sent.append(a) or b"{}"
real_sonos = daemon._renderer.is_sonos
daemon._renderer.is_sonos = lambda: True
try:
    orch = object.__new__(daemon.Orchestrator)
    r = orch.shuffle(True)
finally:
    daemon._renderer.is_sonos = real_sonos
assert r == {"routed": None, "shuffle": None}, r
assert sent == [], "nothing may reach go-librespot from a sonos box"
print("9. sonos: refused, and go-librespot is left alone OK")

print("\nSHUFFLE GESTURE OK — hold play to shuffle, and the top bar "
      "says so without a word.")
