#!/usr/bin/env python3
"""tapbox-ui — the screen daemon for the Pirate Audio HAT (240x240 ST7789,
four buttons). A pure consumer of the tapboxd API (:3679).

Views:  Home (sections) -> Entries -> Episodes -> Now Playing
        Kid mode (settings.simple_nav): the browse hierarchy is replaced
        by ONE flat carousel — a big cover per library entry, B/Y flip,
        X volume, A plays and opens the normal Now Playing view (which
        keeps all its controls); hold-B there returns to the carousel.
        No categories, no reading needed.
        Settings: hold A+B ~2s (parental lock — a kid must not be able to
        shut the box down or wipe caches)

Buttons (BCM 5=A, 6=B, 16=X, 24=Y):
  menus:        A=select  B=back   X=up      Y=down
  now playing:  A=play/pause (the same physical button as select — picking
                             something and pausing it feel like one action)
                B: press=previous, hold=back to the episode list (so back
                   is B everywhere: short in menus, hold here)
                X: press=volume mode (then B=down, Y=up; closes after 3s)
                   hold=switch output (bt speaker <-> built-in)
                Y: press=next, hold=episode picker (the same list the
                   full menus use — also kid mode's hidden episode way)

The battery indicator is drawn in the top-right corner of every view.
The screen blanks after settings.screen_timeout_s (0 = never); the
waking button press is swallowed. Brightness is settings.screen_
brightness (% backlight via PWM on BCM13).

Dev mode (no HAT needed):
  TAPBOX_UI_PNG=/tmp/frame.png   render frames to a PNG instead of SPI
  TAPBOX_UI_INPUT=/tmp/ui-fifo   read button events from a fifo: one char
                                 per event: a/b/x/y = press, l = long-B,
                                 s = settings
"""

import os
import select
import sys
import threading
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, "/usr/local/lib/tapbox-py"):
    if os.path.isdir(os.path.join(_p, "tapbox")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from tapbox import boxapi  # noqa: E402

api_get = boxapi.get
api_post = boxapi.post
api_put = boxapi.put

W = H = 240
PNG_PATH = os.environ.get("TAPBOX_UI_PNG")
FIFO_PATH = os.environ.get("TAPBOX_UI_INPUT")
TICK_S = 0.2
STATUS_POLL_S = 1.0
SYSTEM_POLL_S = 30.0
CONTROL_TIMEOUT = 5   # play/pause/next/prev hit the LOCAL daemon — if it
                      # can't answer in 5s the backend is wedged; fail fast
                      # so buttons keep working instead of freezing the UI
NOW_RETURN_S = 10     # idle this long in a browse menu while music plays ->
                      # snap back to now-playing (once you left it, the only
                      # way back was re-tapping the same episode)

BG = (12, 12, 20)
FG = (235, 235, 235)
DIM = (140, 140, 150)
HILITE = (255, 170, 30)
GOOD = (80, 200, 120)
WARN = (230, 80, 80)


def log(msg):
    print(f"tapbox-ui: {msg}", flush=True)


def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # old pillow
        return ImageFont.load_default()


F_BIG, F_MED, F_SMALL = font(22), font(17), font(13)


# --- display backends -----------------------------------------------------------

BACKLIGHT_PIN = 13  # Pirate Audio backlight (BCM13, PWM1-capable)


class PngDisplay:
    def __init__(self):
        self.path = PNG_PATH
        self.on = True
        self.brightness = 100

    def show(self, img):
        img.save(self.path + ".tmp", "PNG")
        os.replace(self.path + ".tmp", self.path)

    def set_backlight(self, on):
        self.on = on

    def set_brightness(self, pct):
        self.brightness = pct


class St7789Display:
    def __init__(self):
        import st7789  # Pimoroni library
        # backlight=None: we drive BCM13 ourselves so we can DIM it (the
        # library only does on/off). PWMLED via the lgpio pin factory
        # gives real brightness control; only runs while the screen is on.
        self.disp = st7789.ST7789(
            height=240, width=240, rotation=90, port=0, cs=1, dc=9,
            backlight=None, spi_speed_hz=80 * 1000 * 1000)
        self.on = True
        self.brightness = 100
        self._bl = None
        try:
            from gpiozero import PWMLED
            self._bl = PWMLED(BACKLIGHT_PIN)
            self._bl.value = 1.0
        except Exception as e:
            log(f"backlight PWM unavailable ({e.__class__.__name__}) — "
                f"on/off only")

    def show(self, img):
        self.disp.display(img)

    def _apply(self):
        if self._bl is not None:
            self._bl.value = (self.brightness / 100.0) if self.on else 0.0

    def set_backlight(self, on):
        self.on = on
        if self._bl is not None:
            self._apply()
        else:
            # no PWM: fall back to the library's on/off
            self.disp.set_backlight(1 if on else 0)

    def set_brightness(self, pct):
        self.brightness = max(10, min(100, int(pct)))
        self._apply()


def make_display():
    if PNG_PATH:
        log(f"dev display -> {PNG_PATH}")
        return PngDisplay()
    return St7789Display()


# --- input backends ---------------------------------------------------------------

class FifoInput:
    """Dev input: one char per event on a fifo (a/b/x/y press, l=long-B,
    e=long-Y, o=long-X, s=settings)."""

    def __init__(self, path):
        if not os.path.exists(path):
            os.mkfifo(path)
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.gesture_mode = False  # resolved tokens come pre-cooked here
        log(f"dev input <- {path}")

    def poll(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        events = []
        for ch in os.read(self.fd, 64).decode(errors="ignore"):
            if ch in "abxy":
                events.append(ch)
            elif ch == "l":
                events.append("b_long")
            elif ch == "o":
                events.append("x_long")
            elif ch == "e":
                events.append("y_long")
            elif ch == "s":
                events.append("settings")
        return events


class GpioInput:
    """Pirate Audio buttons — event/state logic. Hold A+B ~2s -> settings.

    A and B fire on RELEASE — a press can be the start of the A+B
    combo, and firing them on press made the combo navigate the menu
    while you were holding it (select! back!). Overlapping A+B that
    never reaches HOLD_S is swallowed as a failed combo attempt, not
    delivered as two commands. X/Y stay instant single presses.

    In gesture_mode (the now-playing view) B, X and Y resolve
    short-vs-hold: release before LONG_S -> the plain press, held LONG_S
    -> '<name>_long' (fires while still held — but never while the A+B
    combo is forming)."""

    PINS = {"a": 5, "b": 6, "x": 16, "y": 24}
    HOLD_S = 2.0      # A+B settings combo
    LONG_S = 0.8      # B/X/Y held this long = the hold action

    def __init__(self):
        from gpiozero import Button
        self.buttons = {name: Button(pin, pull_up=True, bounce_time=0.05)
                        for name, pin in self.PINS.items()}
        self.queue = []
        self.gesture_mode = False
        self.down = {}        # name -> press timestamp while held
        self.tainted = set()  # a/b releases to swallow (combo attempt)
        self._long_sent = {}  # name -> the hold already fired
        self._b_gesture = False   # gesture_mode when B was pressed
        self.wake = threading.Event()  # any button activity ends poll()
        for name, btn in self.buttons.items():
            btn.when_pressed = lambda n=name: self._pressed(n)
            btn.when_released = lambda n=name: self._released(n)
        log("gpio buttons ready (BCM 5/6/16/24)")

    def _pressed(self, name):
        self.wake.set()  # end the current poll() immediately
        if name in ("x", "y") and not self.gesture_mode:
            self.queue.append(name)  # menus: instant single presses
            return
        self.down[name] = time.monotonic()
        if name in ("x", "y"):
            # now-playing: short X = volume, held X = output;
            #              short Y = next, held Y = episode picker
            self._long_sent[name] = False
            return
        if name == "b":
            self._long_sent["b"] = False
            # judge the RELEASE by the mode the press STARTED in: b_long
            # navigates away from now-playing while still held, flipping
            # gesture_mode off — the release must not then be re-read as
            # a menu press (field bug: it re-selected the episode)
            self._b_gesture = self.gesture_mode

    def _released(self, name):
        self.wake.set()
        held_since = self.down.pop(name, None)
        if name in self.tainted:
            self.tainted.discard(name)
            return
        if held_since is None:
            return
        if name in ("x", "y"):
            if not self._long_sent.get(name):
                self.queue.append(name)
            return
        other = "b" if name == "a" else "a"
        if other in self.down:
            # overlapping A+B released before HOLD_S: a failed combo
            # attempt, not two commands — swallow the other one too
            self.tainted.add(other)
            return
        if name == "b" and self._b_gesture:
            if not self._long_sent.get("b"):
                self.queue.append("b")
            return
        self.queue.append(name)

    def poll(self, timeout):
        # a button callback sets the event -> instant reaction; otherwise
        # this is the tick for hold-timing (combo, the _long gestures)
        self.wake.wait(timeout)
        self.wake.clear()
        return self._events()

    def _events(self):
        now = time.monotonic()
        if "a" in self.down and "b" in self.down:
            if now - max(self.down["a"], self.down["b"]) >= self.HOLD_S:
                # swallow both releases; drop anything queued meanwhile
                self.tainted.update(self.down)
                self.down.clear()
                self.queue.clear()
                return ["settings"]
        elif (self._b_gesture and "b" in self.down
                and not self._long_sent.get("b")
                and now - self.down["b"] >= self.LONG_S):
            # long press fires while still held — no waiting for release
            self._long_sent["b"] = True
            self.queue.append("b_long")
        for name in ("x", "y"):
            if (name in self.down and not self._long_sent.get(name)
                    and now - self.down[name] >= self.LONG_S):
                self._long_sent[name] = True
                self.queue.append(f"{name}_long")
        ev, self.queue = self.queue[:], []
        return ev


class LgpioInput(GpioInput):
    """Same button logic, but the pins are SAMPLED (20Hz) over raw lgpio
    instead of watched via gpiozero callbacks: the lg alert machinery
    runs a ~1ms-tick thread that burned 13-15% CPU on the Zero around
    the clock (field measurement — one hot thread, screen dark). Four
    gpio_read ioctls every 50ms are unmeasurable, worst-case latency is
    one sample, and 50ms sampling inherently debounces."""

    def __init__(self):
        import lgpio
        self._lg = lgpio
        self._h = None
        for chip in (0, 4):  # main header: chip 0 (chip 4 on a Pi 5)
            try:
                h = lgpio.gpiochip_open(chip)
            except lgpio.error:
                continue
            try:
                for pin in self.PINS.values():
                    lgpio.gpio_claim_input(h, pin, lgpio.SET_PULL_UP)
                self._h = h
                break
            except lgpio.error:
                lgpio.gpiochip_close(h)
        if self._h is None:
            raise RuntimeError("no gpiochip exposes the button pins")
        self.queue = []
        self.gesture_mode = False
        self.down = {}
        self.tainted = set()
        self._long_sent = {}
        self._b_gesture = False
        self.wake = threading.Event()  # set by inherited handlers; unused
        self._level = {n: 1 for n in self.PINS}   # pull-up: 1 = released
        self._edge_at = {n: 0.0 for n in self.PINS}
        log("lgpio buttons ready (BCM 5/6/16/24, 20Hz sampled)")

    def _sample(self):
        now = time.monotonic()
        for name, pin in self.PINS.items():
            lvl = self._lg.gpio_read(self._h, pin)
            if lvl == self._level[name]:
                continue
            self._level[name] = lvl
            if now - self._edge_at[name] < 0.05:
                continue  # contact bounce — swallow the phantom edge
            self._edge_at[name] = now
            if lvl == 0:  # active low
                self._pressed(name)
            else:
                self._released(name)

    def poll(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self._sample()
            if self.queue:
                break  # respond now — don't sit out the tick
            rest = deadline - time.monotonic()
            if rest <= 0:
                break
            time.sleep(min(0.05, rest))
        return self._events()


def make_input():
    if FIFO_PATH:
        return FifoInput(FIFO_PATH)
    try:
        return LgpioInput()
    except Exception as e:
        log(f"lgpio input unavailable ({e.__class__.__name__}: {e}) — "
            f"falling back to gpiozero")
        return GpioInput()


# --- drawing helpers ----------------------------------------------------------------

_BATT_COLOR = [None]  # hysteresis: the PiSugar percent jitters a few
                      # points, and a hard threshold made the gauge flap


def _batt_color(pct, plugged):
    if pct is None:
        return DIM
    if plugged:
        _BATT_COLOR[0] = GOOD
        return GOOD
    lo, mid = 10, 20
    prev = _BATT_COLOR[0]
    if prev == WARN:
        lo += 3    # once red, climb back to orange only at 13
    elif prev == HILITE:
        mid += 3   # once orange, back to green only at 23
    color = WARN if pct <= lo else (HILITE if pct <= mid else GOOD)
    _BATT_COLOR[0] = color
    return color


def battery_corner(draw, system):
    """Battery gauge top-right — on every view. Just the bar (color
    carries the message: green ok/charging, orange <=20, red <=10);
    the exact percent lives in the PWA."""
    pct = (system or {}).get("battery")
    plugged = (system or {}).get("plugged")
    x, y, w, h = W - 32, 8, 24, 11
    color = _batt_color(pct, plugged)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=2, outline=color)
    draw.rectangle([x + w + 1, y + 3, x + w + 2, y + h - 3], fill=color)
    if pct is not None:
        fill = max(2, int((w - 4) * min(pct, 100) / 100))
        draw.rectangle([x + 2, y + 2, x + 2 + fill, y + h - 2], fill=color)


MARQUEE_STEP_S = 0.35  # how fast a too-long selected label slides


def marquee(text, maxlen):
    """(visible_window, scrolling?) for a list label. A too-long SELECTED
    row slides through its text — pause at each end — so the whole name
    can be read, instead of forever showing just the start."""
    if len(text) <= maxlen:
        return text, False
    span = len(text) - maxlen
    period = span + 8  # 4 resting steps at each end
    step = int(time.monotonic() / MARQUEE_STEP_S) % period
    off = max(0, min(span, step - 4))
    return text[off:off + maxlen], True


def draw_list(draw, title, items, sel, system, hint=None, maxlen=24):
    draw.text((10, 4), title, font=F_MED, fill=DIM)
    battery_corner(draw, system)
    top, row_h, visible = 30, 30, 6
    first = max(0, min(sel - 2, len(items) - visible))
    scrolling = False
    for i, item in enumerate(items[first:first + visible]):
        idx = first + i
        y = top + i * row_h
        if idx == sel:
            draw.rounded_rectangle([4, y - 2, W - 4, y + row_h - 6],
                                   radius=6, fill=(40, 40, 60))
        label, right = item if isinstance(item, tuple) else (item, None)
        if idx == sel:
            label, rolls = marquee(label, maxlen)
            scrolling = scrolling or rolls
        else:
            label = label[:maxlen]
        draw.text((14, y), label, font=F_MED,
                  fill=FG if idx == sel else DIM)
        if right:
            draw.text((W - 14, y), right, font=F_MED,
                      fill=HILITE if idx == sel else DIM, anchor="ra")
    if len(items) > visible:
        frac_top = first / len(items)
        frac_bot = (first + visible) / len(items)
        draw.rectangle([W - 5, top + frac_top * 180,
                        W - 3, top + frac_bot * 180], fill=DIM)
    if hint:
        draw.text((10, H - 18), hint, font=F_SMALL, fill=DIM)
    return scrolling


def wrap_two(d, text, fnt, maxw):
    """Split text onto up to two lines (word-wrapped). The second line is
    returned IN FULL — the caller marquees it when it overflows, so the
    part that tells kids' episodes apart is never simply cut off."""
    if d.textlength(text, font=fnt) <= maxw:
        return [text]
    words = text.split()
    first = ""
    while words:
        cand = (first + " " + words[0]).strip()
        if d.textlength(cand, font=fnt) > maxw:
            break
        first = cand
        words.pop(0)
    if not first:  # one monster word — hard-split it
        first = text
        while d.textlength(first, font=fnt) > maxw and len(first) > 1:
            first = first[:-1]
        words = [text[len(first):]]
    return [first, " ".join(words)]


def fmt_time(s):
    if s is None:
        return "--:--"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60}:{s % 60:02d}"


def fmt_bytes(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --- the app ---------------------------------------------------------------------

class App:
    def __init__(self, display, inputs):
        self.display = display
        self.inputs = inputs
        self.view = "home"          # home|entries|episodes|now|settings|storage
        self.stack = []             # (view, sel) breadcrumbs for back
        self.sel = 0
        self.library = {"sections": []}
        self.section = None
        self.expanded = None        # /expand result for current entry
        self.entry = None
        self.status = {}
        self.system = {}
        self.bt = {"devices": []}
        self.bt_found = []
        self.settings = {"screen_timeout_s": 30, "idle_shutdown_min": 30,
                         "volume_cap": 100}
        self.volume_flash = 0.0     # show volume overlay until this time
        self.volume_shown = None
        self.vol_mode_until = 0.0   # while set: B/Y adjust volume (X opened it)
        self.bt_connecting_until = 0.0  # popup X pressed: full connect running
        self.wifi_connecting_until = 0.0  # X pressed: wifi reconnect running
        self.catch_up_until = 0.0   # repaint every tick until this time
        self.last_status = 0.0
        self.last_system = 0.0
        self.last_input = time.monotonic()
        self.user_touched = False
        self.dirty = True
        self.last_render = 0.0
        self.marquee_active = False  # keep repainting while a label slides
        self.car_sel = 0            # kid mode: index into the flat carousel
        self.artwork_cache = {}
        self._art_pending = set()   # remote covers being fetched off-thread
        self._art_fails = {}        # per-cover failure count -> retry backoff
        self._lib_at = 0.0          # last /library fetch (TTL'd)

    # -- data ---------------------------------------------------------------

    def _set(self, attr, value):
        """Repaint only when the data actually changed — every repaint is
        a full PIL compose + a 115KB SPI push, and a paused now-view
        otherwise redraws an identical frame every STATUS_POLL_S."""
        if getattr(self, attr) != value:
            setattr(self, attr, value)
            self.dirty = True

    def refresh(self):
        now = time.monotonic()
        if now - self.last_system > SYSTEM_POLL_S:
            self.last_system = now
            try:
                self._set("system", api_get("/system"))
                self._set("settings", api_get("/settings"))
                # brightness may have changed from the PWA — apply live
                self.display.set_brightness(
                    self.settings.get("screen_brightness", 100))
            except OSError:
                pass
        self._apply_nav_mode()
        if (self.view == "home" and not self.user_touched
                and now - self.last_status > 2.0):
            self.last_status = now
            try:
                self._set("status", api_get("/status"))
            except (OSError, ValueError):
                self._set("status", {})
            if self.status.get("playing"):
                self.stack = [("home", 0)]
                self.view = "now"
                self.dirty = True
        if self.view in ("now", "episodes", "carousel") \
                and now - self.last_status > STATUS_POLL_S:
            self.last_status = now
            try:
                self._set("status", api_get("/status"))
            except OSError:
                self._set("status", {})

    def _apply_nav_mode(self):
        """Follow the simple_nav (kid mode) setting live — flipped in the
        PWA or the box's settings menu. Only browse-side views are
        swapped; an open settings/bt view is left alone and reconciles
        the moment it is left."""
        simple = bool(self.settings.get("simple_nav"))
        if simple and self.view in ("home", "entries"):
            # (now-playing AND the hold-Y episode picker are shared by
            # both modes — left alone)
            self.stack, self.view = [], "carousel"
            self.dirty = True
        elif not simple and self.view == "carousel":
            self.stack, self.view, self.sel = [], "home", 0
            self.dirty = True

    def load_library(self, ttl=2.0):
        """/library with a small TTL: render paths (home + carousel) call
        this per frame, and marquee/progress repaints run a few frames a
        second — one HTTP fetch per repaint was pointless load."""
        now = time.monotonic()
        if now - self._lib_at < ttl and self.library.get("sections"):
            return
        self._lib_at = now
        try:
            self.library = api_get("/library")
        except OSError:
            self.library = {"sections": []}

    def flat_entries(self):
        """Every library entry in order — the kid-mode carousel is flat:
        one big picture per entry, no categories to understand."""
        return [e for s in (self.library or {}).get("sections", [])
                for e in s.get("entries", [])]

    def _art_key(self, ref, size):
        """Cache key for one artwork. Local files carry their mtime, so a
        re-uploaded category logo (same path, new content) refreshes on
        the next render instead of showing the old picture forever."""
        if not ref.startswith("http"):
            try:
                return (ref, size, int(os.path.getmtime(ref)))
            except OSError:
                pass
        return (ref, size)

    def artwork(self, ref, size=110):
        if not ref:
            return None
        key = self._art_key(ref, size)
        cached = self.artwork_cache.get(key)
        if isinstance(cached, float):  # failed earlier — when to retry
            if time.monotonic() < cached:
                return None
        elif key in self.artwork_cache:
            return cached
        try:
            if ref.startswith("http"):
                with urllib.request.urlopen(ref, timeout=10) as r:
                    raw = r.read()
                import io
                img = Image.open(io.BytesIO(raw))
            else:
                img = Image.open(ref)
            img = img.convert("RGB")
            img.thumbnail((size, size))
            # drop stale versions of the same file (older mtime keys)
            for k in [k for k in self.artwork_cache
                      if k[:2] == (ref, size) and k != key]:
                del self.artwork_cache[k]
            self.artwork_cache[key] = img
            self._art_fails.pop(key, None)
            return img
        except Exception as e:
            # Never cache a failure for good — and don't sit on the FIRST
            # failure either: boot is now fast enough that the resume's
            # cover fetch races wifi and loses (URLError seconds before
            # DHCP; field 2026-07-18), and a flat 60s backoff left the
            # mosaic up for a minute+ after the net was fine. Escalate
            # instead: retry in 5s, then 10, 20, 40, capped at 60 — the
            # boot race costs one short beat, a truly dead network still
            # backs off to the old cadence.
            fails = self._art_fails.get(key, 0) + 1
            self._art_fails[key] = fails
            backoff = min(60.0, 5.0 * (2 ** (fails - 1)))
            log(f"artwork failed ({e.__class__.__name__}), retry in "
                f"{backoff:.0f}s: {ref[:80]}")
            self.artwork_cache[key] = time.monotonic() + backoff
            return None

    def artwork_async(self, ref, size=110):
        """artwork() that never touches the network on the render thread:
        a remote cover is fetched in the background and the view repaints
        when it lands. Local files still decode inline."""
        if not ref:
            return None
        if not ref.startswith("http"):
            return self.artwork(ref, size)
        key = self._art_key(ref, size)
        cached = self.artwork_cache.get(key)
        if isinstance(cached, float):  # failed recently — retry when due
            if time.monotonic() < cached:
                return None
        elif key in self.artwork_cache:
            return cached
        if key not in self._art_pending:
            self._art_pending.add(key)

            def fetch():
                try:
                    self.artwork(ref, size)
                finally:
                    self._art_pending.discard(key)
                    self.dirty = True
            threading.Thread(target=fetch, daemon=True).start()
        return None

    def _prewarm_art(self):
        """Decode every carousel/menu cover once, right after boot. Lazy
        decoding made the first pass through the carousel stutter tile by
        tile (a full-size JPEG takes ~0.5s at 600 MHz powersave)."""
        time.sleep(2.0)  # let the first paint and status fetch win the CPU
        for e in self.flat_entries():
            ref = e.get("image")
            if ref and not ref.startswith("http"):
                for size in (176, 56):
                    self.artwork(ref, size)
                time.sleep(0.05)
        self.dirty = True

    def _row_art(self, rows):
        """Cover of the highlighted list row (56px). Loading can hit the
        network for a non-synced show, so wait until scrolling settles
        — self.dirty retries next tick (the loop clears it pre-render)."""
        if not rows:
            return None
        ref = rows[min(self.sel, len(rows) - 1)].get("image")
        if not ref:
            return None
        if (self._art_key(ref, 56) not in self.artwork_cache
                and time.monotonic() - self.last_input < 0.4):
            self.dirty = True
            return None
        return self.artwork(ref, 56)

    def entry_art(self):
        return self._row_art(self.section.get("entries") or [])

    def section_art(self):
        """The highlighted category's uploaded logo on the home screen."""
        return self._row_art((self.library or {}).get("sections") or [])

    # -- input ----------------------------------------------------------------

    def push(self, view):
        self.stack.append((self.view, self.sel))
        self.view, self.sel = view, 0
        self.dirty = True

    def _enter_now(self):
        """Open now-playing right after issuing a play. Force an immediate
        status refetch and repaint every tick for a few seconds: the
        steady-state repaint is change-driven (CPU), but go-librespot
        takes a moment to load the new track and can briefly report an
        unchanged/blank status mid-switch — without this the panel keeps
        showing the previous playlist's cover until the next change or a
        keypress."""
        self.push("now")
        self.last_status = 0.0                     # poll now, not in ~1s
        self.catch_up_until = time.monotonic() + 6

    def _no_internet(self):
        """Instant offline check from the last /system poll — no network
        probe. Only reports offline on positive evidence (hotspot mode, wifi
        off, or a link with no IP); an empty/unpolled status never blocks a
        play. Used to fail Spotify fast instead of hanging."""
        w = self.system.get("wifi")
        if not w:
            return False
        return bool(w.get("hotspot")) or not w.get("enabled") or not w.get("ip")

    def back(self):
        if self.stack:
            self.view, self.sel = self.stack.pop()
        self.dirty = True

    def handle(self, ev):
        self.dirty = True
        self.user_touched = True
        if ev == "settings":
            if self.view != "settings":
                self.push("settings")
            return
        if self.view == "now":
            self.handle_now(ev)
            return
        if self.view == "carousel":
            self.handle_carousel(ev)
            return
        if ev == "b_long":
            ev = "b"  # the hold gesture only means something while playing
        items = self.current_items()
        if ev == "x":
            self.sel = (self.sel - 1) % max(1, len(items))
        elif ev == "y":
            self.sel = (self.sel + 1) % max(1, len(items))
        elif ev == "a":
            self.select()  # A acts everywhere: select here, play/pause in now
        elif ev == "b":
            self.back()    # B backs out everywhere — matching hold-B in now

    def handle_now(self, ev):
        # A = play/pause: the same physical button that selects in the
        # menus — pick something / pause it feel like one action. B is
        # previous (hold = back to the menu, mirroring short-B in menus).
        st = self.status or {}
        if ev == "x" and (st.get("bt_waiting") or st.get("bt_lost")):
            # the popup is modal for X: volume without sound is pointless
            self._bt_connect_last()
            return
        if ev == "a" and (st.get("bt_lost") or st.get("bt_waiting")) \
                and st.get("bt_local_ok"):
            self._play_on_local()  # the popup's "play on box speaker"
            return
        if ev == "x" and st.get("spotify_offline") \
                and st.get("source") == "spotify":
            self._wifi_reconnect()  # X = get the net back now
            return
        in_vol = time.monotonic() < self.vol_mode_until
        try:
            if ev == "a":
                api_post("/playpause", timeout=CONTROL_TIMEOUT)
                self.last_status = 0  # poll immediately
            elif ev == "b":
                if in_vol:  # volume card open: B/Y are - / +
                    self._volume_mode(delta=-5)
                else:
                    api_post("/prev", timeout=CONTROL_TIMEOUT)
                    self.last_status = 0
            elif ev == "b_long":
                self._back_to_episodes()
            elif ev == "x":
                self._volume_mode(delta=None)  # open/extend the volume card
            elif ev == "x_long":
                self._toggle_output()
            elif ev == "y":
                if in_vol:
                    self._volume_mode(delta=5)
                else:
                    api_post("/next", timeout=CONTROL_TIMEOUT)
                    self.last_status = 0
            elif ev == "y_long":
                self._open_episodes()
        except OSError as e:
            log(f"control failed: {e}")

    def _open_episodes(self):
        """Hold-Y in now-playing: the episode picker for whatever is
        playing — the same list view the full menus use. In kid mode this
        is the (deliberately hidden) way to jump between episodes; back
        from the list returns to now-playing."""
        target = (self.status or {}).get("target")
        if not target:
            return
        self.load_library(ttl=0)  # fresh — we might not have browsed yet
        for sec in (self.library or {}).get("sections", []):
            for e in sec.get("entries", []):
                if e.get("target") != target:
                    continue
                self.draw_message("Fetching episodes ...")
                try:
                    self.expanded = api_get(f"/expand?id={e['id']}")
                except (OSError, ValueError):
                    self.draw_message("Network error — try again")
                    time.sleep(1)
                    return
                if not self.expanded.get("episodes"):
                    return  # spotify etc: no episode list exists
                self.section, self.entry = sec, e
                self.push("episodes")
                now_id = (self.status or {}).get("episode_id")
                if now_id:  # land on the playing episode
                    for i, ep in enumerate(self.expanded["episodes"]):
                        if ep.get("id") == now_id:
                            self.sel = i + 1  # row 0 = "Play all"
                            break
                return

    def _back_to_episodes(self):
        """Leave now-playing for the episode list of whatever is playing.
        The stack usually has it — but the auto-jump to now-playing
        resets the stack to [home], which made hold-A land on the home
        screen instead of the episodes (field: 'jumps back several
        pages')."""
        if self.settings.get("simple_nav"):
            # kid mode: the carousel IS the browse level — land on the
            # playing tile
            tgt = (self.status or {}).get("target")
            for i, e in enumerate(self.flat_entries()):
                if e["target"] == tgt:
                    self.car_sel = i
                    break
            self.stack, self.view = [], "carousel"
            self.dirty = True
            return
        if self.stack and self.stack[-1][0] == "episodes":
            self.back()
            return
        target = (self.status or {}).get("target")
        for sec in (self.library or {}).get("sections", []):
            for e in sec.get("entries", []):
                if e.get("target") == target:
                    try:
                        self.expanded = api_get(f"/expand?id={e['id']}")
                    except (OSError, ValueError):
                        break
                    if not self.expanded.get("episodes"):
                        break  # spotify etc: no episode view exists
                    self.section, self.entry = sec, e
                    self.stack = [("home", 0), ("entries", 0)]
                    self.view, self.sel = "episodes", 0
                    self.dirty = True
                    return
        self.back()

    def handle_carousel(self, ev):
        """Kid mode's browse level: B/Y flip through big covers, X is the
        volume card, and A = "play this tile" — one meaning: it resumes
        the entry at its own bookmark and opens the NORMAL now-playing
        view. The daemon makes a replay of what's already loaded a plain
        unpause/no-op, so A never restarts anything. Hold-B in
        now-playing comes back here; settings stay behind the parental
        A+B hold."""
        ents = self.flat_entries()
        if not ents:
            return
        st = self.status or {}
        if ev == "x" and (st.get("bt_waiting") or st.get("bt_lost")):
            # the popup is modal for X: volume without sound is pointless
            self._bt_connect_last()
            return
        if ev == "a" and (st.get("bt_lost") or st.get("bt_waiting")) \
                and st.get("bt_local_ok"):
            self._play_on_local()  # the popup's "play on box speaker"
            return
        in_vol = time.monotonic() < self.vol_mode_until
        try:
            if ev == "y":
                if in_vol:
                    self._volume_mode(delta=5)
                else:
                    self.car_sel = (self.car_sel + 1) % len(ents)
            elif ev in ("b", "b_long"):
                if in_vol:
                    self._volume_mode(delta=-5)
                else:
                    self.car_sel = (self.car_sel - 1) % len(ents)
            elif ev == "a":
                e = ents[self.car_sel % len(ents)]
                if "spotify" in e["target"] and self._no_internet():
                    self._reconnect_for_spotify()  # try to GET the net
                    return
                r = api_post("/play", {"id": e["id"]},
                             timeout=CONTROL_TIMEOUT)
                if r.get("error") == "no-internet":
                    # wifi is up but the WAN is down — the daemon's probe
                    # is the authority (the local check above can't tell)
                    self._reconnect_for_spotify()
                    return
                self._enter_now()
            elif ev == "x":
                self._volume_mode(delta=None)  # open/extend the volume card
        except OSError as e:
            log(f"carousel action failed: {e}")

    def _toggle_output(self):
        """Hold X: flip between the bluetooth speaker and the built-in
        one — the same set_output the PWA buttons use."""
        try:
            cur = api_get("/output").get("output")
            dev = "local" if cur == "bt" else "bt"
            r = api_post("/output", {"device": dev})
        except OSError as e:
            log(f"output toggle failed: {e}")
            return
        name = "built-in speaker" if dev == "local" else "bluetooth speaker"
        self.draw_message(f"Output: {name}"
                          + (" (no sound card?)" if r.get("warning") else ""))
        time.sleep(1.2)   # let it read before the next repaint
        self.dirty = True

    def _volume_mode(self, delta):
        """The volume card: X opens it, then B/Y adjust while it shows."""
        try:
            r = api_get("/volume") if delta is None                 else api_post("/volume", {"delta": delta})
        except OSError as e:
            log(f"volume failed: {e}")
            return
        self.volume_shown = r.get("volume")
        self.vol_mode_until = time.monotonic() + 3.0
        self.volume_flash = self.vol_mode_until

    def current_items(self):
        if self.view == "home":
            return [s["name"] for s in self.library["sections"]] or ["(empty library)"]
        if self.view == "entries":
            return [e["name"] for e in self.section["entries"]]
        if self.view == "episodes":
            eps = self.expanded["episodes"]
            now_id = (self.status or {}).get("episode_id")
            rows = []
            for e in eps:
                playing = now_id is not None and e.get("id") == now_id
                title = e.get("title") or e.get("id") or "?"
                rows.append((("▶ " if playing else "") + title,
                             "✓" if e.get("cached") else ""))
            return ["▶ Play all"] + rows
        if self.view == "settings":
            s = self.settings
            w = self.system.get("wifi") or {}
            wifi = "on" if w.get("enabled") else "off"
            return [("Screen off after", self.fmt_timeout(s["screen_timeout_s"])),
                    ("Brightness", f"{s.get('screen_brightness', 100)}%"),
                    ("Volume cap", f"{s['volume_cap']}%"),
                    ("Auto-off (idle)", self.fmt_idle(s["idle_shutdown_min"])),
                    ("Kid mode", "on" if s.get("simple_nav") else "off"),
                    ("Wi-Fi", wifi),
                    ("Setup hotspot", "on" if w.get("hotspot") else ""),
                    ("Bluetooth", ""),
                    ("Storage", ""),
                    ("Shut down", ""),
                    ("Restart", "")]
        if self.view == "bt":
            rows = [("Pair nearest", ""), ("Scan for new", "")]
            for d in self.bt.get("devices", []):
                mark = "●" if d.get("connected") else (
                    "✓" if d["mac"] == self.bt.get("configured") else "")
                rows.append((d["name"], mark))
            return rows
        if self.view == "btscan":
            return [(d["name"] + (" ♪" if d.get("audio") else ""), "")
                    for d in self.bt_found] or ["(nothing found)"]
        if self.view == "storage":
            return []
        return []

    @staticmethod
    def fmt_timeout(v):
        return "never" if v == 0 else f"{v}s"

    @staticmethod
    def fmt_idle(v):
        return "off" if v == 0 else f"{v} min"

    def select(self):
        try:
            if self.view == "home":
                secs = self.library["sections"]
                if not secs:
                    return
                self.section = secs[self.sel]
                self.push("entries")
            elif self.view == "entries":
                self.entry = self.section["entries"][self.sel]
                self.draw_message("Fetching episodes ...")
                # /expand is instant for Spotify (no network) — resolve first,
                # then guard: Spotify needs the net, so say so instantly
                # instead of spawning a play that just fails in the background.
                self.expanded = api_get(f"/expand?id={self.entry['id']}")
                if self.expanded["kind"] == "spotify" or not self.expanded["episodes"]:
                    if self.expanded["kind"] == "spotify" and self._no_internet():
                        self._reconnect_for_spotify()  # get the net now
                        return
                    r = api_post("/play", {"id": self.entry["id"]})
                    if r.get("error") == "no-internet":
                        # wifi up, WAN down — the daemon's probe knows
                        self._reconnect_for_spotify()
                        return
                    self._enter_now()
                else:
                    self.push("episodes")
            elif self.view == "episodes":
                body = {"id": self.entry["id"]}
                if self.sel > 0:
                    ep = self.expanded["episodes"][self.sel - 1]
                    if ep.get("id"):
                        body["episode"] = ep["id"]
                api_post("/play", body)
                self._enter_now()
            elif self.view == "settings":
                self.select_setting()
            elif self.view == "bt":
                self.select_bt()
            elif self.view == "btscan":
                if self.bt_found:
                    d = self.bt_found[self.sel]
                    self.bt_connect(d["mac"], d["name"])
        except OSError as e:
            log(f"action failed: {e}")
            self.draw_message("Network error — try again")
            time.sleep(1)

    def select_setting(self):
        i = self.sel
        cycles = {0: ("screen_timeout_s", [15, 30, 60, 0]),
                  1: ("screen_brightness", [25, 50, 75, 100]),
                  2: ("volume_cap", [60, 70, 80, 90, 100]),
                  3: ("idle_shutdown_min", [15, 30, 60, 0]),
                  4: ("simple_nav", [1, 0])}  # kid mode on/off
        if i in cycles:
            key, opts = cycles[i]
            cur = self.settings.get(key)
            nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
            self.settings = api_put("/settings", {key: nxt})
            if key == "screen_brightness":
                self.display.set_brightness(nxt)  # live preview
        elif i == 5:
            enabled = (self.system.get("wifi") or {}).get("enabled")
            self.draw_message("Please wait ...")
            r = api_post("/system/wifi", {"enabled": not enabled})
            self.system.setdefault("wifi", {}).update(r)
        elif i == 6:
            # Setup hotspot from the BOX: the only way in at a new place
            # when saved networks aren't around — the PWA needs a shared
            # network, which is exactly what's missing (chicken-and-egg).
            # Joining the AP pops the phone's captive portal into the PWA.
            hs = bool((self.system.get("wifi") or {}).get("hotspot"))
            if hs:
                self.draw_message("Stopping hotspot ...")
                api_post("/wifi/hotspot", {"enabled": False}, timeout=45)
                time.sleep(1.2)
            else:
                # start_hotspot scans FIRST (the radio can't scan in AP
                # mode) — that is most of the wait
                self.draw_message("Starting hotspot ...\n(scanning, ~30 s)")
                r = api_post("/wifi/hotspot", {"enabled": True}, timeout=90)
                if r.get("ok"):
                    self.draw_message(f"On your phone, join\n"
                                      f"“{r.get('ssid')}”\n"
                                      f"password: {r.get('password')}")
                    time.sleep(8)  # long enough to actually read it
                else:
                    self.draw_message("Hotspot failed — try again")
                    time.sleep(1.5)
            self.last_system = 0.0  # refresh the hotspot state row now
        elif i == 7:
            self.draw_message("Loading speakers ...")
            self.bt = api_get("/bt")
            self.push("bt")
        elif i == 8:
            self.push("storage")
        elif i in (9, 10):
            # row 9 = Shut down, row 10 = Restart (an inverted flag here
            # made Restart power the box off — field-reported)
            action = "Restarting" if i == 10 else "Shutting down"
            self.draw_message(f"{action} ... (A confirms, B cancels)")
            if self.confirm():
                self.draw_message(f"{action} ...")
                api_post("/system/shutdown", {"restart": i == 10})

    def select_bt(self):
        if self.sel == 0:  # Pair nearest (the one-button flow)
            self.draw_message("Pairing the nearest speaker ... (up to 60 s)")
            try:
                r = api_post("/bt/pair", {}, timeout=130)
                self.bt = {k: r[k] for k in ("configured", "devices", "pairing")
                           if k in r} or api_get("/bt")
                self.draw_message("Paired!" if r.get("ok")
                                  else (r.get("output") or "Failed").splitlines()[-1])
            except OSError as e:
                self.draw_message(f"Failed: {e}")
            time.sleep(2)
        elif self.sel == 1:  # Scan for new
            self.draw_message("Scanning ... (~25 s)")
            try:
                r = api_post("/bt/scan", {}, timeout=70)
                self.bt_found = r.get("found", [])
                self.push("btscan")
            except OSError as e:
                self.draw_message(f"Failed: {e}")
                time.sleep(2)
        else:
            d = self.bt["devices"][self.sel - 2]
            self.bt_connect(d["mac"], d["name"])

    def bt_connect(self, mac, name):
        self.draw_message(f"Connecting to {name} ...")
        try:
            r = api_post("/bt/connect", {"mac": mac}, timeout=120)
            for k in ("configured", "devices", "pairing"):
                if k in r:
                    self.bt[k] = r[k]
            self.draw_message("Connected!" if r.get("ok")
                              else (r.get("output") or "Failed").splitlines()[-1])
        except OSError as e:
            self.draw_message(f"Failed: {e}")
        time.sleep(2)
        if self.view == "btscan":
            self.back()

    def confirm(self, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for ev in self.inputs.poll(0.1):
                if ev == "a":   # A acts, B backs out — same as everywhere
                    return True
                if ev == "b":
                    return False
        return False

    # -- rendering ----------------------------------------------------------------

    def splash(self, sub="starting"):
        """Boot screen: drawn the moment the process starts, long before
        tapboxd (and the rest of the boot) is ready."""
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2 - 16), "TapBox", font=F_BIG, fill=HILITE,
               anchor="mm")
        d.text((W // 2, H // 2 + 18), sub, font=F_SMALL, fill=DIM, anchor="mm")
        self.display.show(img)

    def draw_message(self, text):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2), text, font=F_MED, fill=FG, anchor="mm")
        battery_corner(d, self.system)
        self.display.show(img)

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        rolls = False  # a too-long selected label is sliding -> keep painting
        if self.view == "home":
            self.load_library()
            art = self.section_art()  # uploaded category logo (PWA)
            rolls = draw_list(d, "TapBox", self.current_items(), self.sel,
                              self.system,
                              hint="A: select   hold A+B: settings",
                              maxlen=17 if art else 24)
            if art:
                img.paste(art, (W - art.width - 6, 26))
        elif self.view == "entries":
            art = self.entry_art()
            rolls = draw_list(d, self.section["name"], self.current_items(),
                              self.sel, self.system, maxlen=17 if art else 24)
            if art:
                img.paste(art, (W - art.width - 6, 26))
        elif self.view == "episodes":
            rolls = draw_list(d, self.expanded.get("name") or "Episoder",
                              self.current_items(), self.sel, self.system,
                              hint="✓ = downloaded (plays offline)")
        elif self.view == "settings":
            rolls = draw_list(d, "Settings", self.current_items(), self.sel,
                              self.system, hint="A: change   B: back")
        elif self.view == "bt":
            rolls = draw_list(d, "Bluetooth speaker", self.current_items(),
                              self.sel, self.system,
                              hint="● connected   ✓ selected")
        elif self.view == "btscan":
            rolls = draw_list(d, "Nearby devices", self.current_items(),
                              self.sel, self.system,
                              hint="A: pair and connect   B: back")
        elif self.view == "storage":
            self.render_storage(d)
        elif self.view == "carousel":
            self.load_library()
            rolls = self.render_carousel(d, img)
        elif self.view == "now":
            rolls = self.render_now(d, img)
        self.marquee_active = bool(rolls)
        self.display.show(img)

    def render_storage(self, d):
        d.text((10, 4), "Storage", font=F_MED, fill=DIM)
        battery_corner(d, self.system)
        y = 40
        disk = self.system.get("disk") or {}
        rows = []
        if disk:
            used = disk["total"] - disk["free"]
            rows.append(("SD card", f"{fmt_bytes(used)} / {fmt_bytes(disk['total'])}"))
            rows.append(("Free", fmt_bytes(disk["free"])))
        for name, size in (self.system.get("caches") or {}).items():
            label = "Podcast cache" if name == "podcasts" else "Spotify cache"
            rows.append((label, fmt_bytes(size)))
        wifi = self.system.get("wifi") or {}
        rows.append(("Wi-Fi", wifi.get("ssid") or "—"))
        rows.append(("IP", wifi.get("ip") or "—"))
        if self.system.get("cpu_temp") is not None:
            rows.append(("CPU temp", f"{self.system['cpu_temp']}°C"))
        for label, val in rows:
            d.text((12, y), label, font=F_MED, fill=DIM)
            d.text((W - 12, y), val, font=F_MED, fill=FG, anchor="ra")
            y += 26

    def render_now(self, d, img):
        st = self.status or {}
        battery_corner(d, self.system)
        # (no-internet is a POPUP now — _net_overlay at the end — matching
        # the BT-disconnect popup instead of a thin banner; field ask
        # 2026-07-18)
        # Cover priority: a per-item image already on disk (podcast
        # episode art) is instant; a remote cover (Spotify album art,
        # gfx.nrk.no episode art) is fetched OFF the render thread so it
        # never blocks or stalls the UI, and lands on a later repaint;
        # meanwhile the cached collection cover (show cover / playlist
        # mosaic) fills in immediately so the card is never blank —
        # offline or while the remote loads.
        ep_art = st.get("artwork")
        local = st.get("artwork_local")
        art = None
        if ep_art and not str(ep_art).startswith("http"):
            art = self.artwork(ep_art, 128)
        if art is None and ep_art and str(ep_art).startswith("http"):
            art = self.artwork_async(ep_art, 128)  # non-blocking; may be None
        if art is None and local:
            art = self.artwork(local, 128)  # offline-proof fallback
        if art:
            img.paste(art, ((W - art.width) // 2, 24))
            ty = 156
        else:
            ty = 70
        title = st.get("title") or "(nothing playing)"
        # width capped so the text never runs under the side markers
        # (they sit at the physical button heights, x < 22)
        rolls = False
        if st.get("source") == "spotify":
            # Spotify: ONE line — sliding when too long — so the artist
            # is ALWAYS visible beneath (field pick). Only when spotify
            # is the ACTIVE source: /status keeps the paused-spotify
            # block around during mpv playback, and its last artist has
            # nothing to do with the podcast episode showing.
            if d.textlength(title, font=F_MED) > W - 44:
                title, rolls = marquee(title, 20)
            d.text((W // 2, ty), title, font=F_MED, fill=FG, anchor="ma")
            sub = ", ".join((st.get("spotify") or {}).get("artists") or [])
            if sub:
                d.text((W // 2, ty + 22), sub[:30], font=F_SMALL,
                       fill=DIM, anchor="ma")
        else:
            # podcasts: up to two lines (long episode names matter most);
            # a tail that doesn't fit even then slides, never cut off
            lines = wrap_two(d, title, F_MED, W - 44)
            d.text((W // 2, ty), lines[0], font=F_MED, fill=FG, anchor="ma")
            if len(lines) > 1:
                l2 = lines[1]
                if d.textlength(l2, font=F_MED) > W - 44:
                    l2, rolls = marquee(l2, 20)
                d.text((W // 2, ty + 19), l2, font=F_MED, fill=FG,
                       anchor="ma")
        pos, dur = st.get("position"), st.get("duration")
        bar_y = H - 34  # below the B/Y markers (y 178-192) — no overlap
        d.rectangle([14, bar_y, W - 14, bar_y + 5], fill=(50, 50, 65))
        if pos and dur:
            frac = max(0.0, min(1.0, pos / dur))
            d.rectangle([14, bar_y, 14 + frac * (W - 28), bar_y + 5], fill=HILITE)
        left = fmt_time(pos) if pos is not None else "--:--"
        right = "live" if (pos is not None and dur is None) else fmt_time(dur)
        d.text((14, bar_y + 10), left, font=F_SMALL, fill=DIM)
        d.text((W - 14, bar_y + 10), right, font=F_SMALL, fill=DIM, anchor="ra")
        # Button markers sit where the PHYSICAL buttons are — the same
        # spots the carousel uses (A/X centers ~y=55, B/Y ~y=185, hugging
        # the screen edges). Drawn shapes: DejaVu has no media glyphs.
        # A (top left): play/pause — THE action, in the highlight color
        if st.get("playing"):
            d.rectangle([6, 47, 10, 63], fill=HILITE)
            d.rectangle([14, 47, 18, 63], fill=HILITE)
        else:
            d.polygon([(5, 47), (5, 63), (19, 55)], fill=HILITE)
        # X (top right): volume
        d.polygon([(W - 19, 52), (W - 13, 52), (W - 6, 46),
                   (W - 6, 64), (W - 13, 58), (W - 19, 58)], fill=DIM)
        # B (bottom left): previous |<
        d.rectangle([5, 178, 7, 192], fill=DIM)
        d.polygon([(19, 178), (19, 192), (9, 185)], fill=DIM)
        # Y (bottom right): next >|
        d.polygon([(W - 19, 178), (W - 19, 192), (W - 9, 185)], fill=DIM)
        d.rectangle([W - 7, 178, W - 5, 192], fill=DIM)
        self._volume_overlay(d)
        if not self._bt_overlay(d):  # speaker trouble outranks net trouble
            self._net_overlay(d)
        return rolls

    def _volume_overlay(self, d):
        """The transient volume card (X opened it; B/Y adjust)."""
        if time.monotonic() < self.volume_flash:
            d.rounded_rectangle([50, 84, 190, 136], radius=8, fill=(30, 30, 45))
            shown = "–" if self.volume_shown is None else self.volume_shown
            d.text((W // 2, 92), f"Volume {shown}", font=F_MED,
                   fill=HILITE, anchor="ma")
            d.text((60, 116), "B  -", font=F_SMALL, fill=DIM)
            d.text((W - 60, 116), "+ Y", font=F_SMALL, fill=DIM, anchor="ra")

    def _bt_overlay(self, d):
        """Speaker-state popup, driven entirely by /status (field log
        2026-07-17: the speaker came up 25s before anyone pressed play —
        nobody KNEW it was ready). bt_waiting = a play attempt hit a
        disconnected speaker: tell them, and offer X = a full connect of
        the configured device (incl. crash recovery — stronger than the
        kick that already happened). The daemon flips it to bt_ready the
        moment the transport is up: 'press A'. Painted LAST, over the
        volume card; self-clears because the daemon expires both states."""
        st = self.status or {}
        if st.get("bt_lost"):
            # the speaker DIED mid-play and the daemon stopped playback
            # (mpv skips episodes wildly into a dead device otherwise)
            d.rounded_rectangle([22, 70, W - 22, 156], radius=10,
                                fill=(45, 30, 30))
            d.text((W // 2, 80), "Speaker disconnected", font=F_MED,
                   fill=WARN, anchor="ma")
            hint = ("connecting..." if time.monotonic()
                    < self.bt_connecting_until else "X: reconnect")
            d.text((W // 2, 108), hint, font=F_SMALL, fill=FG, anchor="ma")
            if st.get("bt_local_ok"):
                d.text((W // 2, 130), "A: play on box speaker",
                       font=F_SMALL, fill=DIM, anchor="ma")
            return True
        if st.get("bt_waiting"):
            # identical shape to the bt_lost popup: X connects the
            # speaker, A plays on the built-in one instead (where present)
            d.rounded_rectangle([22, 70, W - 22, 156], radius=10,
                                fill=(45, 30, 30))
            d.text((W // 2, 80), "Speaker not connected", font=F_MED,
                   fill=WARN, anchor="ma")
            hint = ("connecting..." if time.monotonic()
                    < self.bt_connecting_until else "X: connect now")
            d.text((W // 2, 108), hint, font=F_SMALL, fill=FG, anchor="ma")
            if st.get("bt_local_ok"):
                d.text((W // 2, 130), "A: play on box speaker",
                       font=F_SMALL, fill=DIM, anchor="ma")
            return True
        if st.get("bt_ready"):
            d.rounded_rectangle([22, 82, W - 22, 144], radius=10,
                                fill=(28, 45, 30))
            d.text((W // 2, 92), "Speaker connected!", font=F_MED,
                   fill=HILITE, anchor="ma")
            d.text((W // 2, 118), "Press A to play", font=F_SMALL, fill=FG,
                   anchor="ma")
            return True
        return False

    def _net_overlay(self, d):
        """No-internet popup for an active Spotify source — the SAME
        shape as the speaker popups (field ask 2026-07-18: the thin text
        banner over the album art read as decoration, not as something
        to act on). X runs the on-demand wifi reconnect, exactly like X
        reconnects the speaker on the BT popup. Only when spotify is the
        active source: cached podcasts play fine offline and must not get
        a scary popup."""
        st = self.status or {}
        if not (st.get("spotify_offline") and st.get("source") == "spotify"):
            return False
        d.rounded_rectangle([22, 70, W - 22, 156], radius=10,
                            fill=(45, 30, 30))
        d.text((W // 2, 80), "No internet", font=F_MED,
               fill=WARN, anchor="ma")
        hint = ("reconnecting Wi-Fi..." if time.monotonic()
                < self.wifi_connecting_until else "X: reconnect Wi-Fi")
        d.text((W // 2, 108), hint, font=F_SMALL, fill=FG, anchor="ma")
        d.text((W // 2, 130), "Spotify needs internet",
               font=F_SMALL, fill=DIM, anchor="ma")
        return True

    def _play_on_local(self):
        """The speaker popup's A action: drop back to the built-in
        speaker. If nothing's sounding (bt_lost stopped it, or a fresh
        play attempt), resume from the bookmark; if audio is ALREADY
        playing on the built-in one (you switched the output to a
        disconnected BT speaker), just make the output local — don't
        toggle playpause and pause it. Fire-and-forget; the popup clears
        via /status once the output is no longer bt."""
        was_playing = bool((self.status or {}).get("playing"))

        def go():
            try:
                api_post("/output", {"device": "local"}, timeout=30)
                if not was_playing:
                    api_post("/playpause", timeout=CONTROL_TIMEOUT)
            except OSError as e:
                log(f"play-on-local failed: {e}")
            self.last_status = 0
        threading.Thread(target=go, daemon=True).start()

    def _reconnect_for_spotify(self):
        """Pressing play on a Spotify tile with no net: don't dead-end
        with 'can't play' — that IS an explicit 'get me the net'. Run the
        reconnect (blocking, with a message, like the pair flow), then
        play if it worked."""
        self.draw_message("No internet —\nreconnecting Wi-Fi ...")
        try:
            r = api_post("/wifi/reconnect", {"secs": 30}, timeout=45)
        except OSError:
            r = {}
        self.last_system = 0  # refresh wifi state
        if not r.get("ok"):
            self.draw_message("Still no internet —\ntry again later")
            time.sleep(1.5)
            self.dirty = True

    def _wifi_reconnect(self):
        """The offline-Spotify popup's X action: fire-and-forget on-demand
        wifi reconnect (daemon quiesces A2DP, waits for a known network,
        unparks go-librespot on success). Progress shows in the banner;
        the daemon 409s overlaps, so mashing X is harmless."""
        if time.monotonic() < self.wifi_connecting_until:
            return
        self.wifi_connecting_until = time.monotonic() + 35

        def go():
            try:
                api_post("/wifi/reconnect", {"secs": 30}, timeout=45)
            except (OSError, ValueError):
                pass
            self.wifi_connecting_until = 0.0
            self.last_status = 0  # re-poll: banner clears when back online
            self.last_system = 0
        threading.Thread(target=go, daemon=True).start()

    def _bt_connect_last(self):
        """The popup's X action: fire-and-forget full connect of the
        configured speaker (bt.py use — includes firmware-crash
        recovery). Progress comes back via /status; the daemon 409s
        overlapping attempts, so mashing X is harmless."""
        if time.monotonic() < self.bt_connecting_until:
            return
        self.bt_connecting_until = time.monotonic() + 60

        def go():
            try:
                mac = (api_get("/bt", timeout=10) or {}).get("configured")
                if mac:
                    api_post("/bt/connect", {"mac": mac}, timeout=120)
            except (OSError, ValueError):
                pass
            self.bt_connecting_until = 0.0
        threading.Thread(target=go, daemon=True).start()

    def render_carousel(self, d, img):
        """Kid mode: ONE big cover per entry — flip with B/Y, play with A.
        The carousel doubles as now-playing: the playing entry shows its
        play state and a progress bar. Returns the marquee flag."""
        battery_corner(d, self.system)
        ents = self.flat_entries()
        if not ents:
            d.text((W // 2, H // 2), "Library is empty", font=F_MED,
                   fill=DIM, anchor="mm")
            return False
        self.car_sel %= len(ents)
        e = ents[self.car_sel]
        if "spotify" in e["target"] \
                and (self.status or {}).get("spotify_offline"):
            # warn BEFORE the kid presses play on a tile that can't work
            d.text((10, 4), "No internet", font=F_SMALL, fill=WARN)
        art = self.artwork_async(e.get("image"), 176)
        ax, ay = (W - 176) // 2, 24
        if art:
            img.paste(art, ((W - art.width) // 2, ay))
        else:
            # no cover: a colored tile with the entry's initial — stable
            # color per name so kids can still recognise "their" tile
            palette = [(196, 92, 82), (206, 148, 70), (98, 158, 88),
                       (84, 138, 186), (142, 108, 178), (186, 98, 140)]
            color = palette[sum(e["name"].encode()) % len(palette)]
            d.rounded_rectangle([ax, ay, ax + 176, ay + 176], radius=14,
                                fill=color)
            d.text((W // 2, ay + 88), (e["name"][:1] or "?").upper(),
                   font=font(96), fill=FG, anchor="mm")
        # Markers sit where the PHYSICAL buttons are: the Pirate Audio
        # buttons are inset from the screen corners — centers land around
        # y=55 (A/X) and y=185 (B/Y) on the 240px panel (field-calibrated;
        # corner-aligned markers pointed well past the actual buttons).
        # flip chevrons < > (B / Y), dim outlines hugging the screen edges
        d.line([(17, 177), (6, 185), (17, 193)], fill=DIM, width=3,
               joint="curve")
        d.line([(W - 17, 177), (W - 6, 185), (W - 17, 193)], fill=DIM,
               width=3, joint="curve")
        # A (top left): play the selected tile — THE action here, so it
        # gets the highlight color; hugs the edge like the chevrons
        d.polygon([(5, 47), (5, 63), (19, 55)], fill=HILITE)
        name, rolls = marquee(e["name"], 20)
        d.text((W // 2, 206), name, font=F_MED, fill=FG, anchor="ma")
        st = self.status or {}
        if st.get("target") == e["target"]:
            # this tile is what's (or was) playing: a thick orange
            # underline beneath the name (playing or paused alike) — a
            # frame around the art read as clutter, per field feedback
            tl = d.textlength(name, font=F_MED)
            d.rounded_rectangle([(W - tl) / 2, 228, (W + tl) / 2, 232],
                                radius=2, fill=HILITE)
            pos, dur = st.get("position"), st.get("duration")
            if pos and dur:
                frac = max(0.0, min(1.0, pos / dur))
                d.rectangle([ax, ay + 172, ax + 176, ay + 176],
                            fill=(50, 50, 65))
                d.rectangle([ax, ay + 172, ax + frac * 176, ay + 176],
                            fill=HILITE)
        self._volume_overlay(d)
        self._bt_overlay(d)
        return rolls

    # -- main loop -------------------------------------------------------------------

    def screen_should_sleep(self):
        # The timeout applies whether on charger or battery (0 = never
        # blank). No special charger behaviour — the screen just blanks
        # after screen_timeout_s of no button input, always.
        t = self.settings.get("screen_timeout_s", 30)
        if t == 0:
            return False
        return time.monotonic() - self.last_input > t

    def run(self):
        # Show the splash immediately, then wait for tapboxd — during boot
        # it is usually a few seconds behind us.
        ticks = 0
        while True:
            try:
                # gate on /settings only (a local file read — always fast);
                # /system can take seconds at boot (pisugar, go-librespot
                # flapping) and kept the splash up long after playback ran
                self.settings = api_get("/settings", timeout=2)
                break
            except (OSError, ValueError):
                self.splash("starting" + "." * (ticks % 4))
                ticks += 1
                time.sleep(0.7)
        try:
            self.system = api_get("/system", timeout=3)
        except (OSError, ValueError):
            pass  # refresh() fills it in on the next tick
        self.display.set_brightness(self.settings.get("screen_brightness", 100))
        self.load_library()
        threading.Thread(target=self._prewarm_art, daemon=True).start()
        # Come back where we were: a live session (boot resume) or a
        # bookmarked-paused ghost puts the screen straight on now-playing.
        try:
            self.status = api_get("/status", timeout=3)
        except (OSError, ValueError):
            self.status = {}
        if self.settings.get("simple_nav"):
            # kid mode: carousel is the root, positioned on whatever is
            # (or last was) playing — which opens on now-playing if live
            tgt = self.status.get("target")
            for i, e in enumerate(self.flat_entries()):
                if e["target"] == tgt:
                    self.car_sel = i
                    break
            if self.status.get("title"):
                self.stack, self.view = [("carousel", 0)], "now"
            else:
                self.stack, self.view = [], "carousel"
        elif self.status.get("title"):
            self.stack = [("home", 0)]
            self.view = "now"
        log("ready")
        while True:
            self.inputs.gesture_mode = (self.view == "now")
            # Screen off = deep idle: long ticks, and a button press sets
            # the wake event so poll() returns INSTANTLY — no latency, and
            # 8x fewer wakeups than the old 0.6s polling
            events = self.inputs.poll(TICK_S if self.display.on else 5.0)
            if events:
                woke = not self.display.on
                self.last_input = time.monotonic()
                if woke:
                    self.display.set_backlight(True)
                    self.last_system = 0.0   # refetch battery/system now
                    self.last_status = 0.0
                    self.dirty = True  # swallow the waking press
                else:
                    for ev in events:
                        self.handle(ev)
            if self.display.on:
                # No data polling while the screen is dark — there is
                # nothing to update, and 1/s status HTTP all night is
                # pure battery waste.
                self.refresh()
                # Browsing went idle while something plays: snap back to
                # now-playing. Only from the browse views — settings/BT
                # flows have their own long waits (scan, pair) and must
                # not be yanked away from.
                if (self.view in ("home", "entries", "episodes", "carousel")
                        and self.status.get("playing")
                        and time.monotonic() - self.last_input
                        > NOW_RETURN_S):
                    self.push("now")
            if self.display.on and self.screen_should_sleep():
                self.display.set_backlight(False)
                if PNG_PATH:  # dev: make the blanking visible
                    self.display.show(Image.new("RGB", (W, H), (0, 0, 0)))
            elif self.display.on and (self.dirty
                                      or time.monotonic() < self.catch_up_until
                                      or (self.marquee_active
                                          and time.monotonic()
                                          - self.last_render >= MARQUEE_STEP_S)
                                      or (self.last_render < self.volume_flash
                                          and time.monotonic()
                                          - self.last_render >= 0.5)):
                # Repaints are change-driven (_set marks dirty): while
                # playing the 1s status poll moves the progress bar, while
                # paused NOTHING repaints — a full PIL compose + 115KB SPI
                # push per identical frame was measurable CPU on the Zero.
                # Time-based exceptions: the volume overlay (paint until one
                # frame lands after it expired) and a sliding long label in
                # the menus (marquee_active, ~3 fps while selected).
                self.dirty = False
                self.last_render = time.monotonic()
                self.render()


def _boot_splash(display):
    """Light the panel with the boot splash the instant the display is up
    — BEFORE the slower input (lgpio) init — so the screen shows life
    early instead of staying blank through the rest of startup. Guarded:
    a splash must never block the real UI coming up."""
    try:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((W // 2, H // 2 - 16), "TapBox", font=F_BIG, fill=HILITE,
               anchor="mm")
        d.text((W // 2, H // 2 + 18), "starting", font=F_SMALL, fill=DIM,
               anchor="mm")
        display.show(img)
    except Exception as e:
        log(f"boot splash skipped: {e!r}")


def main():
    display = make_display()
    _boot_splash(display)          # screen lights up now, not after input init
    app = App(display, make_input())
    app.run()


if __name__ == "__main__":
    main()
