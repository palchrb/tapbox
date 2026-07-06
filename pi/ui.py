#!/usr/bin/env python3
"""tapbox-ui — the screen daemon for the Pirate Audio HAT (240x240 ST7789,
four buttons). A pure consumer of the tapboxd API (:3679).

Views:  Home (sections) -> Entries -> Episodes -> Now Playing
        Settings: hold A+B ~2s (parental lock — a kid must not be able to
        shut the box down or wipe caches)

Buttons (BCM 5=A, 6=B, 16=X, 24=Y):
  menus:        A=select  B=back   X=up      Y=down
  now playing:  A=play/pause  B=back  X=vol+  Y=vol-

The battery indicator is drawn in the top-right corner of every view.
The screen blanks after settings.screen_timeout_s (0 = never; always on
while charging); the waking button press is swallowed.

Dev mode (no HAT needed):
  TAPBOX_UI_PNG=/tmp/frame.png   render frames to a PNG instead of SPI
  TAPBOX_UI_INPUT=/tmp/ui-fifo   read button events from a fifo: one char
                                 per event: a/b/x/y = press, s = settings
"""

import json
import os
import select
import sys
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

DAEMON = os.environ.get("TAPBOX_DAEMON", "http://127.0.0.1:3679")
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


def api_get(path, timeout=10):
    with urllib.request.urlopen(DAEMON + path, timeout=timeout) as r:
        return json.loads(r.read())


def api_post(path, obj=None, timeout=15):
    req = urllib.request.Request(
        DAEMON + path, data=json.dumps(obj or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def api_put(path, obj):
    req = urllib.request.Request(
        DAEMON + path, data=json.dumps(obj).encode(), method="PUT",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


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
    """Dev input: one char per event on a fifo (a/b/x/y press, s=settings)."""

    def __init__(self, path):
        if not os.path.exists(path):
            os.mkfifo(path)
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        log(f"dev input <- {path}")

    def poll(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        events = []
        for ch in os.read(self.fd, 64).decode(errors="ignore"):
            if ch in "abxy":
                events.append(ch)
            elif ch == "s":
                events.append("settings")
        return events


class GpioInput:
    """Pirate Audio buttons via gpiozero. Hold A+B ~2s -> settings."""

    PINS = {"a": 5, "b": 6, "x": 16, "y": 24}
    HOLD_S = 2.0

    def __init__(self):
        from gpiozero import Button
        self.buttons = {name: Button(pin, pull_up=True, bounce_time=0.05)
                        for name, pin in self.PINS.items()}
        self.queue = []
        self.combo_since = None
        for name, btn in self.buttons.items():
            btn.when_pressed = lambda n=name: self.queue.append(n)
        log("gpio buttons ready (BCM 5/6/16/24)")

    def poll(self, timeout):
        time.sleep(timeout)
        a, b = self.buttons["a"].is_pressed, self.buttons["b"].is_pressed
        if a and b:
            if self.combo_since is None:
                self.combo_since = time.monotonic()
            elif time.monotonic() - self.combo_since >= self.HOLD_S:
                self.combo_since = None
                self.queue.clear()  # eat the presses that formed the combo
                return ["settings"]
        else:
            self.combo_since = None
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


def draw_list(draw, title, items, sel, system, hint=None):
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
        draw.text((14, y), label[:24], font=F_MED,
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
        self.settings = {"screen_timeout_s": 30, "idle_shutdown_min": 30,
                         "volume_cap": 100}
        self.volume_flash = 0.0     # show volume overlay until this time
        self.volume_shown = None
        self.last_status = 0.0
        self.last_system = 0.0
        self.last_input = time.monotonic()
        self.dirty = True
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
        if self.view == "now" and now - self.last_status > STATUS_POLL_S:
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

    def artwork(self, ref):
        if not ref:
            return None
        if ref not in self.artwork_cache:
            try:
                if ref.startswith("http"):
                    with urllib.request.urlopen(ref, timeout=10) as r:
                        raw = r.read()
                    import io
                    img = Image.open(io.BytesIO(raw))
                else:
                    img = Image.open(ref)
                img = img.convert("RGB")
                img.thumbnail((110, 110))
                self.artwork_cache[ref] = img
            except Exception:
                self.artwork_cache[ref] = None
        return self.artwork_cache[ref]

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
        if ev == "settings":
            if self.view != "settings":
                self.push("settings")
            return
        if self.view == "now":
            self.handle_now(ev)
            return
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
        try:
            if ev == "a":
                api_post("/playpause")
                self.last_status = 0  # poll immediately
            elif ev == "b":
                self.back()
            elif ev in ("x", "y"):
                r = api_post("/volume", {"delta": 5 if ev == "x" else -5})
                self.volume_shown = r.get("volume")
                self.volume_flash = time.monotonic() + 1.5
        except OSError as e:
            log(f"control failed: {e}")

    def current_items(self):
        if self.view == "home":
            return [s["name"] for s in self.library["sections"]] or ["(empty library)"]
        if self.view == "entries":
            return [e["name"] for e in self.section["entries"]]
        if self.view == "episodes":
            eps = self.expanded["episodes"]
            return ["▶ Play all"] + [
                (e.get("title") or e.get("id") or "?",
                 "✓" if e.get("cached") else "") for e in eps]
        if self.view == "settings":
            s = self.settings
            wifi = "on" if (self.system.get("wifi") or {}).get("enabled") else "off"
            return [("Screen off after", self.fmt_timeout(s["screen_timeout_s"])),
                    ("Volume cap", f"{s['volume_cap']}%"),
                    ("Auto-off (idle)", self.fmt_idle(s["idle_shutdown_min"])),
                    ("Wi-Fi", wifi),
                    ("Storage", ""),
                    ("Shut down", ""),
                    ("Restart", "")]
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
            self.push("storage")
        elif i in (5, 6):
            action = "Restarting" if i == 6 else "Shutting down"
            self.draw_message(f"{action} ... (A confirms, B cancels)")
            if self.confirm():
                self.draw_message(f"{action} ...")
                api_post("/system/shutdown", {"restart": i == 6})

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
            draw_list(d, self.section["name"], self.current_items(), self.sel,
                      self.system)
        elif self.view == "episodes":
            draw_list(d, self.expanded.get("name") or "Episoder",
                      self.current_items(), self.sel, self.system,
                      hint="✓ = downloaded (plays offline)")
        elif self.view == "settings":
            draw_list(d, "Settings", self.current_items(), self.sel,
                      self.system, hint="A: change   B: back")
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
        if time.monotonic() < self.volume_flash and self.volume_shown is not None:
            d.rounded_rectangle([60, 90, 180, 130], radius=8, fill=(30, 30, 45))
            d.text((W // 2, 100), f"Volume {self.volume_shown}", font=F_MED,
                   fill=HILITE, anchor="ma")

    # -- main loop -------------------------------------------------------------------

    def screen_should_sleep(self):
        t = self.settings.get("screen_timeout_s", 30)
        if t == 0:
            return False
        if self.system.get("plugged"):
            return False  # on the charger: always on (nightstand mode)
        return time.monotonic() - self.last_input > t

    def run(self):
        self.load_library()
        try:
            self.system = api_get("/system")
            self.settings = api_get("/settings")
        except OSError:
            pass
        log("ready")
        while True:
            events = self.inputs.poll(TICK_S)
            if events:
                woke = not self.display.on
                self.last_input = time.monotonic()
                if woke:
                    self.display.set_backlight(True)
                    self.dirty = True  # swallow the waking press
                else:
                    for ev in events:
                        self.handle(ev)
            self.refresh()
            if self.display.on and self.screen_should_sleep():
                self.display.set_backlight(False)
                if PNG_PATH:  # dev: make the blanking visible
                    self.display.show(Image.new("RGB", (W, H), (0, 0, 0)))
            elif self.display.on and (self.dirty
                                      or self.view == "now"
                                      or time.monotonic() < self.volume_flash):
                self.dirty = False
                self.render()


def main():
    app = App(make_display(), make_input())
    app.run()


if __name__ == "__main__":
    main()
