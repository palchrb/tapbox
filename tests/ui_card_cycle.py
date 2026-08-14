#!/usr/bin/env python3
"""X cycles a strip of cards: volume, seek, shuffle.

Shuffle used to hang off a hold on play, and seek did not exist at all.
Both are now tabs on the card X already opened for volume, because the
box had run out of gestures: A is play/pause and half the settings
combo, B is previous and back, X is volume and the output picker, Y is
next and the episode picker. A cycle costs no new gesture — but a hidden
sequence of presses has to be LEARNED, so the tab strip is what makes it
honest. Three tabs, one lit: 'there are others' the first time you see
it.

Two rules carry the whole design, and both are pinned below:

  A LAPSED CARD REOPENS ON VOLUME (pin 3). X has meant volume for
  months and that reflex must never land somewhere else. It is also why
  CARD_TTL_S had to grow to five seconds — at three, with a press
  counting only on RELEASE and a hold threshold under it, a child had
  about two seconds of thinking time per press, which made the third tab
  unreachable rather than merely slow.

  THE CARD NEVER FOLLOWS YOU OUT (pin 6). In the browse views X is
  volume and nothing else; a seek card that leaked there would rebind
  B/Y away from flipping tiles, silently.

Pins 11-13 are inherited from the gesture this replaces and pin exactly
what SURVIVED it: the top-bar glyph, the /status fold every icon reads,
and the daemon's refusal to shuffle a Sonos room."""
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

POSTS, GETS = [], []
VOL = [40]


def fake_post(p, b=None, timeout=None):
    POSTS.append((p, b))
    if p == "/volume":
        VOL[0] = max(0, min(100, VOL[0] + (b or {}).get("delta", 0)))
    return {"routed": "mpv", "volume": VOL[0],
            "position": (b or {}).get("position"),
            "shuffle": (b or {}).get("enabled")}


ui.api_post = fake_post
ui.api_get = lambda p, timeout=None: GETS.append(p) or {"volume": VOL[0]}


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
    down = {}

    def poll(self, timeout):
        return []


def app_now(**status):
    a = ui.App(FakeDisplay(), FakeInputs())
    a.view = "now"
    a.system = {"battery": 50}
    a.status = {"title": "Noe", "playing": True, "duration": 1800.0,
                "position": 600.0, "target": "t1", "episode_id": "e1",
                **status}
    return a


def settle(n=1, key=None):
    for _ in range(60):
        if len(POSTS) >= n:
            break
        time.sleep(0.05)


# 1. a cold X opens the VOLUME card — tab 0, every time
app = app_now()
app.handle_now("x")
assert app._card() == "vol", app._card()
assert app.card_idx == 0
print("1. a cold X opens the volume card OK")

# 2. X again walks the strip, and WRAPS. Wrapping is the point: a child
#    who overshoots presses on rather than waiting out the timeout.
seen = []
for _ in range(4):
    app.handle_now("x")
    seen.append(app._card())
assert seen == ["seek", "shuf", "vol", "seek"], seen
print("2. X cycles volume -> seek -> shuffle -> volume OK")

# 3. THE REFLEX RULE: once the card has lapsed, X starts over at volume —
#    it does not resume where the cycle left off
app.vol_mode_until = 0.0
assert app._card() is None, "the card must lapse"
app.handle_now("x")
assert app._card() == "vol", f"a lapsed card must reopen on volume, got {app._card()}"
assert ui.CARD_TTL_S >= 5.0, "three seconds could not carry three tabs"
print("3. a lapsed card reopens on volume, never mid-cycle OK")

# 4. B and Y mean whatever the showing card says they mean
POSTS.clear()
app = app_now()
app.handle_now("x")                      # volume card
for _ in range(60):                      # the /volume read is off-thread
    if app.volume_shown is not None:
        break
    time.sleep(0.05)
app.handle_now("y")
assert app.volume_shown == 45, app.volume_shown   # optimistic +5 off 40
app.handle_now("x")                      # -> seek
app.handle_now("y")
settle(1)
assert POSTS and POSTS[-1][0] == "/seek", POSTS
assert POSTS[-1][1]["position"] == 630.0, POSTS[-1]   # 600 + first step
app.handle_now("x")                      # -> shuffle
POSTS.clear()
app.handle_now("y")
settle(1)
assert POSTS[-1] == ("/shuffle", {"enabled": True}), POSTS
POSTS.clear()
app.handle_now("b")                      # B is OFF, not a toggle
settle(1)
assert POSTS[-1] == ("/shuffle", {"enabled": False}), POSTS
print("4. B/Y route per card, and shuffle is off/on rather than a toggle OK")

# 5. A IS NEVER REBOUND. It is play/pause on every card — the one
#    control a child finds without looking, and a mode error there costs
#    "the pause button didn't pause".
POSTS.clear()
for _ in range(3):
    app.handle_now("a")
    settle(len(POSTS) + 0 or 1)
    app.handle_now("x")
assert {p for p, _ in POSTS} == {"/playpause"}, POSTS
print("5. A stays play/pause on every card OK")

# 6. THE CARD NEVER FOLLOWS YOU OUT: in a browse view it is always the
#    volume card, whatever tab was showing when the user left
app = app_now()
for _ in range(2):
    app.handle_now("x")
assert app._card() == "seek"
app.view = "carousel"
assert app._card() == "vol", "seek must not rebind B/Y in the carousel"
app.view = "now"
assert app._card() == "seek", "and it is still there on the way back"
print("6. outside now-playing the card is always volume OK")


# 7. the settings combo still wins, and no long-press fires en route.
#    This is the invariant the removed A-hold used to threaten; it
#    guards b_long/x_long/y_long just as much.
def mk(now_view=True):
    inp = object.__new__(ui.GpioInput)
    inp.queue, inp.down, inp.tainted = [], {}, set()
    inp._long_sent = {}
    inp._b_gesture = False
    inp.b_hold = False
    inp.gesture_mode = now_view
    inp.wake = threading.Event()
    return inp


T = [1000.0]
real_mono = time.monotonic
ui.time.monotonic = lambda: T[0]
try:
    inp = mk()
    inp._pressed("a")
    T[0] += 0.3
    inp._pressed("b")
    for _ in range(6):
        T[0] += 0.2
        assert inp._events() == [], "no long-press while the combo forms"
    T[0] += ui.GpioInput.HOLD_S
    assert inp._events() == ["settings"], "the combo must still work"
    print("7. A+B opens settings with no long-press en route OK")

    # 8. and A no longer holds AT ALL: it is a plain press everywhere,
    #    which is what frees it from ever racing the combo again
    inp = mk()
    inp._pressed("a")
    T[0] += ui.GpioInput.LONG_S + 1.0
    assert inp._events() == [], "A must not produce a hold any more"
    inp._released("a")
    assert inp._events() == ["a"], "and the press is still play/pause"
    print("8. A has no hold gesture left OK")
finally:
    ui.time.monotonic = real_mono

# 9. seek steps GROW while the presses keep coming, and a reversal
#    resets them — overshoot by five minutes and press back, you land
#    30s away rather than another five minutes away
POSTS.clear()
app = app_now()
app.handle_now("x")
app.handle_now("x")                      # seek card
for _ in range(3):
    app.handle_now("y")
settle(3)
steps = [p[1]["position"] for p in POSTS if p[0] == "/seek"]
assert steps == [630.0, 690.0, 810.0], steps    # +30, +60, +120
app.handle_now("b")                      # reversal
settle(4)
back = [p[1]["position"] for p in POSTS if p[0] == "/seek"][-1]
assert back == 780.0, f"a reversal must start over at 30s, got {back}"
assert app.seek_step_i == 0
print("9. seek accelerates, and a reversal starts over small OK")

# 10. the optimistic position holds until a poll CONFIRMS it — and it is
#     track-scoped, or skipping to the next episode would show 0:00
#     masked behind the old spot. Confirmation is a window, not equality:
#     a correct report is the target plus whatever played since.
app = app_now()
app._pos_expect, app._pos_at = 900.0, time.monotonic()
app._pos_until = app._pos_at + 10
app._pos_key = ("t1", "e1")
app._set("status", {**app.status, "position": 12.0})
assert app.status["position"] == 900.0, "a pre-seek report must not win"
app._set("status", {**app.status, "position": 901.0})
assert app._pos_expect is None, "a report inside the window confirms it"
app._pos_expect, app._pos_key = 900.0, ("t1", "e1")
app._pos_until = time.monotonic() + 10
app._set("status", {**app.status, "position": 3.0, "episode_id": "e2"})
assert app._pos_expect is None, "a new track drops the expectation"
assert app.status["position"] == 3.0
print("10. the optimistic position holds, confirms on a window, and is "
      "track-scoped OK")

# 10b. a modal popup DISMISSES the card. Its box is bigger than the
#      card's, so the card would keep owning B/Y while invisible —
#      merely odd for volume, but an invisible seek loses your place.
app = app_now()
app.handle_now("x")
app.handle_now("x")
assert app._card() == "seek"
app._set("status", {**app.status, "bt_lost": True})
assert app._card() is None, "a speaker popup must close the card"
print("10b. a modal popup dismisses the card underneath it OK")

# 11. the glyph is drawn only when shuffle is on, and the top bar is
#     otherwise byte-identical — absence IS the off state
base = {"battery": 60, "plugged": False, "wifi": {"ip": "1.2.3.4"},
        "bt_ready": True}


def bar(shuffle):
    img = Image.new("RGB", (ui.W, 26), ui.BG)
    ui.battery_corner(ui._draw(img), {**base, "shuffle": shuffle})
    return img


off, on = bar(False), bar(True)
assert off.tobytes() != on.tobytes(), "shuffle-on must look different"
assert bar(False).tobytes() == off.tobytes(), "off must be stable"
box = [i for i in range(ui.W)
       if off.crop((i, 0, i + 1, 26)).tobytes()
       != on.crop((i, 0, i + 1, 26)).tobytes()]
assert box and max(box) < ui.W - 60, \
    f"the glyph must sit left of the existing icons, changed at {box}"
print("11. the glyph appears only when on, left of the other icons OK")

# 12. the daemon's shuffle state reaches self.system through the same
#     fold bt_connected uses — the icon row reads self.system only
app = app_now()
app.system = {"battery": 50}
app._set("status", {"bt_connected": True, "shuffle": True, "playing": True})
assert app.system.get("shuffle") is True
assert app.system.get("bt_ready") is True, "must not break the bt fold"
app._set("status", {"bt_connected": True, "shuffle": False, "playing": True})
assert app.system.get("shuffle") is False
print("12. /status shuffle folds into system beside bt_ready OK")

# 13. the daemon refuses on a sonos renderer instead of quietly aiming
#     the command at go-librespot in another room
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
print("13. sonos: refused, and go-librespot is left alone OK")

print("\nCARD CYCLE OK — one gesture, three cards, and a strip that says "
      "the other two are there.")
