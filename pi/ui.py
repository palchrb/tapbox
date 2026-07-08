#!/usr/bin/env python3
"""tapbox-ui — the screen daemon for the Pirate Audio HAT (240x240 ST7789,
four buttons). A pure consumer of the tapboxd API (:3679).

Views:  Home (sections) -> Entries -> Episodes -> Now Playing
        Settings: hold A+B ~2s (parental lock — a kid must not be able to
        shut the box down or wipe caches)

Buttons (BCM 5=A, 6=B, 16=X, 24=Y):
  menus:        A=select  B=back   X=up      Y=down
  now playing:  A: press=play/pause, hold=back to menu
                X: volume mode (then B=down, Y=up; closes after 3s)
                B=previous  Y=next  (instant single presses)

The battery indicator is drawn in the top-right corner of every view.
The screen blanks after settings.screen_timeout_s (0 = never; always on
while charging); the waking button press is swallowed.

Dev mode (no HAT needed):
  TAPBOX_UI_PNG=/tmp/frame.png   render frames to a PNG instead of SPI
  TAPBOX_UI_INPUT=/tmp/ui-fifo   read button events from a fifo: one char
                                 per event: a/b/x/y = press, l = long-A,
                                 s = settings
"""

import os
import select
import sys
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

class PngDisplay:
    def __init__(self, path):
        self.path = path
        self.on = True

    def show(self, img):
        img.save(self.path + ".tmp", "PNG")
        os.replace(self.path + ".tmp", self.path)

    def set_backlight(self, on):
        self.on = on


class St7789Display:
    def __init__(self):
        import st7789  # Pimoroni library
        self.disp = st7789.ST7789(
            height=240, width=240, rotation=90, port=0, cs=1, dc=9,
            backlight=13, spi_speed_hz=80 * 1000 * 1000)
        self.on = True

    def show(self, img):
        self.disp.display(img)

    def set_backlight(self, on):
        self.disp.set_backlight(1 if on else 0)
        self.on = on


def make_display():
    if PNG_PATH:
        log(f"dev display -> {PNG_PATH}")
        return PngDisplay(PNG_PATH)
    return St7789Display()


# --- input backends ---------------------------------------------------------------

class FifoInput:
    """Dev input: one char per event on a fifo (a/b/x/y press, l=long-A,
    s=settings)."""

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
                events.append("a_long")
            elif ch == "s":
                events.append("settings")
        return events


class GpioInput:
    """Pirate Audio buttons via gpiozero. Hold A+B ~2s -> settings.

    A and B fire on RELEASE — a press can be the start of the A+B
    combo, and firing them on press made the combo navigate the menu
    while you were holding it (select! back!). Overlapping A+B that
    never reaches HOLD_S is swallowed as a failed combo attempt, not
    delivered as two commands. X/Y stay instant single presses.

    In gesture_mode (the now-playing view) A resolves short-vs-hold:
    release before LONG_S -> 'a', held LONG_S -> 'a_long' (fires while
    still held — but never while B is also down: that's a combo)."""

    PINS = {"a": 5, "b": 6, "x": 16, "y": 24}
    HOLD_S = 2.0      # A+B settings combo
    LONG_S = 0.8      # A held this long = back to menu

    def __init__(self):
        from gpiozero import Button
        self.buttons = {name: Button(pin, pull_up=True, bounce_time=0.05)
                        for name, pin in self.PINS.items()}
        self.queue = []
        self.gesture_mode = False
        self.down = {}        # a/b -> press timestamp while held
        self.tainted = set()  # a/b releases to swallow (combo attempt)
        self._a_long_sent = False
        for name, btn in self.buttons.items():
            btn.when_pressed = lambda n=name: self._pressed(n)
        for name in ("a", "b"):
            self.buttons[name].when_released = \
                lambda n=name: self._released(n)
        log("gpio buttons ready (BCM 5/6/16/24)")

    def _pressed(self, name):
        if name in ("x", "y"):
            self.queue.append(name)
            return
        self.down[name] = time.monotonic()
        if name == "a":
            self._a_long_sent = False

    def _released(self, name):
        held_since = self.down.pop(name, None)
        if name in self.tainted:
            self.tainted.discard(name)
            return
        if held_since is None:
            return
        other = "b" if name == "a" else "a"
        if other in self.down:
            # overlapping A+B released before HOLD_S: a failed combo
            # attempt, not two commands — swallow the other one too
            self.tainted.add(other)
            return
        if name == "a" and self.gesture_mode:
            if not self._a_long_sent:
                self.queue.append("a")
            return
        self.queue.append(name)

    def poll(self, timeout):
        time.sleep(timeout)
        now = time.monotonic()
        if "a" in self.down and "b" in self.down:
            if now - max(self.down.values()) >= self.HOLD_S:
                # swallow both releases; drop anything queued meanwhile
                self.tainted.update(self.down)
                self.down.clear()
                self.queue.clear()
                return ["settings"]
        elif (self.gesture_mode and "a" in self.down
                and not self._a_long_sent
                and now - self.down["a"] >= self.LONG_S):
            # long press fires while still held — no waiting for release
            self._a_long_sent = True
            self.queue.append("a_long")
        ev, self.queue = self.queue[:], []
        return ev


def make_input():
    if FIFO_PATH:
        return FifoInput(FIFO_PATH)
    return GpioInput()


# --- drawing helpers ----------------------------------------------------------------

def battery_corner(draw, system):
    """Battery pill in the top-right corner — on every view."""
    pct = (system or {}).get("battery")
    plugged = (system or {}).get("plugged")
    x, y, w, h = W - 46, 6, 34, 15
    color = DIM if pct is None else (
        GOOD if plugged or pct > 30 else (HILITE if pct > 12 else WARN))
    draw.rounded_rectangle([x, y, x + w, y + h], radius=3, outline=color)
    draw.rectangle([x + w + 1, y + 4, x + w + 3, y + h - 4], fill=color)
    if pct is not None:
        fill = max(2, int((w - 4) * min(pct, 100) / 100))
        draw.rectangle([x + 2, y + 2, x + 2 + fill, y + h - 2], fill=color)
        label = "chg" if plugged else f"{int(round(pct))}%"
        draw.text((x - 4, y + 1), label, font=F_SMALL, fill=color, anchor="ra")
    else:
        draw.text((x - 4, y + 1), "?", font=F_SMALL, fill=color, anchor="ra")


def draw_list(draw, title, items, sel, system, hint=None, maxlen=24):
    draw.text((10, 4), title, font=F_MED, fill=DIM)
    battery_corner(draw, system)
    top, row_h, visible = 30, 30, 6
    first = max(0, min(sel - 2, len(items) - visible))
    for i, item in enumerate(items[first:first + visible]):
        idx = first + i
        y = top + i * row_h
        if idx == sel:
            draw.rounded_rectangle([4, y - 2, W - 4, y + row_h - 6],
                                   radius=6, fill=(40, 40, 60))
        label, right = item if isinstance(item, tuple) else (item, None)
        draw.text((14, y), label[:maxlen], font=F_MED,
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
        self.last_status = 0.0
        self.last_system = 0.0
        self.last_input = time.monotonic()
        self.user_touched = False
        self.dirty = True
        self.last_render = 0.0
        self.artwork_cache = {}

    # -- data ---------------------------------------------------------------

    def refresh(self):
        now = time.monotonic()
        if now - self.last_system > SYSTEM_POLL_S:
            self.last_system = now
            try:
                self.system = api_get("/system")
                self.settings = api_get("/settings")
            except OSError:
                pass
            self.dirty = True
        if (self.view == "home" and not self.user_touched
                and now - self.last_status > 2.0):
            self.last_status = now
            try:
                self.status = api_get("/status")
            except (OSError, ValueError):
                self.status = {}
            if self.status.get("playing"):
                self.stack = [("home", 0)]
                self.view = "now"
                self.dirty = True
        if self.view in ("now", "episodes") \
                and now - self.last_status > STATUS_POLL_S:
            self.last_status = now
            try:
                self.status = api_get("/status")
            except OSError:
                self.status = {}
            self.dirty = True

    def load_library(self):
        try:
            self.library = api_get("/library")
        except OSError:
            self.library = {"sections": []}

    def artwork(self, ref, size=110):
        if not ref:
            return None
        key = (ref, size)
        if key not in self.artwork_cache:
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
                self.artwork_cache[key] = img
            except Exception:
                self.artwork_cache[key] = None
        return self.artwork_cache[key]

    def entry_art(self):
        """Cover of the highlighted entry (56px). Loading can hit the
        network for a non-synced show, so wait until scrolling settles
        — self.dirty retries next tick (the loop clears it pre-render)."""
        ents = self.section.get("entries") or []
        if not ents:
            return None
        ref = ents[min(self.sel, len(ents) - 1)].get("image")
        if not ref:
            return None
        if ((ref, 56) not in self.artwork_cache
                and time.monotonic() - self.last_input < 0.4):
            self.dirty = True
            return None
        return self.artwork(ref, 56)

    # -- input ----------------------------------------------------------------

    def push(self, view):
        self.stack.append((self.view, self.sel))
        self.view, self.sel = view, 0
        self.dirty = True

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
        if ev == "a_long":
            ev = "a"  # the hold gesture only means something while playing
        items = self.current_items()
        if ev == "x":
            self.sel = (self.sel - 1) % max(1, len(items))
        elif ev == "y":
            self.sel = (self.sel + 1) % max(1, len(items))
        elif ev == "b":
            self.back()
        elif ev == "a":
            self.select()

    def handle_now(self, ev):
        in_vol = time.monotonic() < self.vol_mode_until
        try:
            if ev == "a":
                api_post("/playpause")
                self.last_status = 0  # poll immediately
            elif ev == "a_long":
                self.back()
            elif ev == "x":
                self._volume_mode(delta=None)  # open/extend the volume card
            elif ev in ("b", "y"):
                if in_vol:
                    self._volume_mode(delta=-5 if ev == "b" else 5)
                else:
                    api_post("/prev" if ev == "b" else "/next")
                    self.last_status = 0
        except OSError as e:
            log(f"control failed: {e}")

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
            wifi = "on" if (self.system.get("wifi") or {}).get("enabled") else "off"
            return [("Screen off after", self.fmt_timeout(s["screen_timeout_s"])),
                    ("Volume cap", f"{s['volume_cap']}%"),
                    ("Auto-off (idle)", self.fmt_idle(s["idle_shutdown_min"])),
                    ("Wi-Fi", wifi),
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
                self.expanded = api_get(f"/expand?id={self.entry['id']}")
                if self.expanded["kind"] == "spotify" or not self.expanded["episodes"]:
                    api_post("/play", {"id": self.entry["id"]})
                    self.push("now")
                else:
                    self.push("episodes")
            elif self.view == "episodes":
                body = {"id": self.entry["id"]}
                if self.sel > 0:
                    ep = self.expanded["episodes"][self.sel - 1]
                    if ep.get("id"):
                        body["episode"] = ep["id"]
                api_post("/play", body)
                self.push("now")
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
                  1: ("volume_cap", [60, 70, 80, 90, 100]),
                  2: ("idle_shutdown_min", [15, 30, 60, 0])}
        if i in cycles:
            key, opts = cycles[i]
            cur = self.settings[key]
            nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
            self.settings = api_put("/settings", {key: nxt})
        elif i == 3:
            enabled = (self.system.get("wifi") or {}).get("enabled")
            self.draw_message("Please wait ...")
            r = api_post("/system/wifi", {"enabled": not enabled})
            self.system.setdefault("wifi", {}).update(r)
        elif i == 4:
            self.draw_message("Loading speakers ...")
            self.bt = api_get("/bt")
            self.push("bt")
        elif i == 5:
            self.push("storage")
        elif i in (6, 7):
            action = "Restarting" if i == 7 else "Shutting down"
            self.draw_message(f"{action} ... (A confirms, B cancels)")
            if self.confirm():
                self.draw_message(f"{action} ...")
                api_post("/system/shutdown", {"restart": i == 6})

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
                if ev == "a":
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
        if self.view == "home":
            self.load_library()
            draw_list(d, "TapBox", self.current_items(), self.sel, self.system,
                      hint="A: select   hold A+B: settings")
        elif self.view == "entries":
            art = self.entry_art()
            draw_list(d, self.section["name"], self.current_items(), self.sel,
                      self.system, maxlen=17 if art else 24)
            if art:
                img.paste(art, (W - art.width - 6, 26))
        elif self.view == "episodes":
            draw_list(d, self.expanded.get("name") or "Episoder",
                      self.current_items(), self.sel, self.system,
                      hint="✓ = downloaded (plays offline)")
        elif self.view == "settings":
            draw_list(d, "Settings", self.current_items(), self.sel,
                      self.system, hint="A: change   B: back")
        elif self.view == "bt":
            draw_list(d, "Bluetooth speaker", self.current_items(), self.sel,
                      self.system, hint="● connected   ✓ selected")
        elif self.view == "btscan":
            draw_list(d, "Nearby devices", self.current_items(), self.sel,
                      self.system, hint="A: pair and connect   B: back")
        elif self.view == "storage":
            self.render_storage(d)
        elif self.view == "now":
            self.render_now(d, img)
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
        art = self.artwork(st.get("artwork"))
        if art:
            img.paste(art, ((W - art.width) // 2, 30))
            ty = 150
        else:
            ty = 70
        title = st.get("title") or "(nothing playing)"
        d.text((W // 2, ty), title[:26], font=F_MED, fill=FG, anchor="ma")
        sub = ", ".join((st.get("spotify") or {}).get("artists") or [])
        if sub:
            d.text((W // 2, ty + 24), sub[:30], font=F_SMALL, fill=DIM, anchor="ma")
        pos, dur = st.get("position"), st.get("duration")
        bar_y = H - 46
        d.rectangle([14, bar_y, W - 14, bar_y + 5], fill=(50, 50, 65))
        if pos and dur:
            frac = max(0.0, min(1.0, pos / dur))
            d.rectangle([14, bar_y, 14 + frac * (W - 28), bar_y + 5], fill=HILITE)
        left = fmt_time(pos) if pos is not None else "--:--"
        right = "live" if (pos is not None and dur is None) else fmt_time(dur)
        d.text((14, bar_y + 10), left, font=F_SMALL, fill=DIM)
        d.text((W - 14, bar_y + 10), right, font=F_SMALL, fill=DIM, anchor="ra")
        # Drawn shapes, not glyphs — DejaVu lacks the media symbols
        cy = bar_y + 16
        if st.get("playing"):
            d.rectangle([W // 2 - 7, cy - 7, W // 2 - 2, cy + 7], fill=FG)
            d.rectangle([W // 2 + 2, cy - 7, W // 2 + 7, cy + 7], fill=FG)
        else:
            d.polygon([(W // 2 - 6, cy - 8), (W // 2 - 6, cy + 8),
                       (W // 2 + 8, cy)], fill=FG)
        # volume-button hint: a small speaker by the X button (top right,
        # below the battery pill)
        d.polygon([(W - 26, 30), (W - 20, 30), (W - 13, 24),
                   (W - 13, 42), (W - 20, 36), (W - 26, 36)], fill=DIM)
        if time.monotonic() < self.volume_flash:
            d.rounded_rectangle([50, 84, 190, 136], radius=8, fill=(30, 30, 45))
            shown = "–" if self.volume_shown is None else self.volume_shown
            d.text((W // 2, 92), f"Volume {shown}", font=F_MED,
                   fill=HILITE, anchor="ma")
            d.text((60, 116), "B  -", font=F_SMALL, fill=DIM)
            d.text((W - 60, 116), "+ Y", font=F_SMALL, fill=DIM, anchor="ra")

    # -- main loop -------------------------------------------------------------------

    def screen_should_sleep(self):
        t = self.settings.get("screen_timeout_s", 30)
        if t == 0:
            return False
        if self.system.get("plugged"):
            return False  # on the charger: always on (nightstand mode)
        return time.monotonic() - self.last_input > t

    def run(self):
        # Show the splash immediately, then wait for tapboxd — during boot
        # it is usually a few seconds behind us.
        ticks = 0
        while True:
            try:
                self.system = api_get("/system", timeout=2)
                self.settings = api_get("/settings", timeout=2)
                break
            except (OSError, ValueError):
                self.splash("starting" + "." * (ticks % 4))
                ticks += 1
                time.sleep(0.7)
        self.load_library()
        # Come back where we were: a live session (boot resume) or a
        # bookmarked-paused ghost puts the screen straight on now-playing.
        try:
            self.status = api_get("/status", timeout=3)
        except (OSError, ValueError):
            self.status = {}
        if self.status.get("title"):
            self.stack = [("home", 0)]
            self.view = "now"
        log("ready")
        while True:
            self.inputs.gesture_mode = (self.view == "now")
            # Screen off = deep idle: slower ticks (presses are queued by
            # gpio interrupts, so nothing is lost — wake latency <=0.6s)
            events = self.inputs.poll(TICK_S if self.display.on else 0.6)
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
            if self.display.on and self.screen_should_sleep():
                self.display.set_backlight(False)
                if PNG_PATH:  # dev: make the blanking visible
                    self.display.show(Image.new("RGB", (W, H), (0, 0, 0)))
            elif self.display.on and (self.dirty
                                      or ((self.view == "now"
                                           or time.monotonic() < self.volume_flash)
                                          and time.monotonic() - self.last_render >= 1.0)):
                # now-playing repaints at 1fps (the progress bar has second
                # granularity) — 5fps PIL+SPI would eat 20-30% CPU on a Zero
                self.dirty = False
                self.last_render = time.monotonic()
                self.render()


def main():
    app = App(make_display(), make_input())
    app.run()


if __name__ == "__main__":
    main()
