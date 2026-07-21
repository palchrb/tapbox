#!/usr/bin/env python3
"""pipod UI + wheel router — DRAFT (untested).

An iPod-style scrolling-list front-end for the click wheel. It is a pure
consumer of the TapBox daemon API (:3679) via `tapbox.boxapi`, plus a UDP
listener for wheel events from clickwheel/click.c.

Why new (not TapBox's ui.py): ui.py is a 4-discrete-button, 240x240 model.
A wheel wants scroll-to-navigate + center-select + menu-back, and an iPod
screen is 320x240. This file is the wheel paradigm; it deliberately reuses
ui.py's album-art disk cache + marquee ideas (copy them in when you flesh
out rendering).

Input (UDP 127.0.0.1:9090, 3 bytes from click.c):
    [idx, state, pos]   idx 0xFF = scroll (pos = absolute wheel position)
                        idx 0..4 = Center/Menu/Play/Prev/Next, state 1=press

Nav model:
    Home:  Now Playing | Music | Podcasts | Settings
    scroll -> move selection (accelerates with speed)
    Center -> enter / select ;  Menu -> back
    Play/Prev/Next -> transport (routed to boxapi, like pi/buttons.py)

Lock: holdswitch.py writes LOCK_FILE; while present, wheel input is ignored
and the screen dims (true iPod Hold behaviour).

Dev mode (no TFT):  PIPOD_UI_PNG=/tmp/pod.png renders frames to a PNG.
"""

import os
import socket
import sys
import time

# Reuse the TapBox python package (boxapi etc.) exactly like pi/buttons.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "..", "pi"), "/usr/local/lib/tapbox-py"):
    if os.path.isdir(os.path.join(_p, "tapbox")):
        sys.path.insert(0, os.path.abspath(_p))
        break
from tapbox import boxapi  # noqa: E402

UDP_PORT = 9090
LOCK_FILE = os.environ.get("PIPOD_LOCK", "/run/pipod-hold.lock")
BATTERY_FILE = os.environ.get("PIPOD_BATTERY", "/run/pipod-battery.json")
W, H = 320, 240
PNG_PATH = os.environ.get("PIPOD_UI_PNG")

# Wheel: 96 physical steps around the ring (0x00..0xBE). We convert absolute
# position deltas into selection movement, with a little acceleration.
WHEEL_MAX = 0xBE
SCROLL_ACCEL_MS = 120   # consecutive steps faster than this move >1 row

# Button indices from click.c
BTN_CENTER, BTN_MENU, BTN_PLAY, BTN_PREV, BTN_NEXT = range(5)
SCROLL = 0xFF


def log(msg):
    print(f"podui: {msg}", flush=True)


def locked():
    return os.path.exists(LOCK_FILE)


def battery_pct():
    """Battery % from battery.py's MAX17048 reader (None if unavailable)."""
    try:
        import json
        with open(BATTERY_FILE) as f:
            return json.load(f).get("percent")
    except (OSError, ValueError):
        return None


# --- menu model -------------------------------------------------------------

class Menu:
    """A scrollable list. `items` are (label, action) where action is a
    callable (enter) or a submenu-building callable."""

    def __init__(self, title, build):
        self.title = title
        self.build = build          # () -> list[(label, callable|Menu-factory)]
        self.items = []
        self.sel = 0

    def load(self):
        try:
            self.items = self.build() or []
        except Exception as e:            # daemon slow/down: show a stub
            log(f"load {self.title} failed: {e}")
            self.items = [("(unavailable)", None)]
        self.sel = min(self.sel, max(0, len(self.items) - 1))

    def move(self, delta):
        if not self.items:
            return
        self.sel = max(0, min(len(self.items) - 1, self.sel + delta))


def _home_items():
    return [
        ("Now Playing", "now"),
        ("Music",       lambda: _library_menu("music")),
        ("Podcasts",    lambda: _library_menu("podcast")),
        ("Settings",    lambda: _settings_menu()),
    ]


def _library_menu(kind):
    """Build a menu from the daemon's library, filtered by kind."""
    lib = boxapi.get("/library", timeout=2.0) or {}
    entries = [e for e in lib.get("entries", []) if e.get("kind", "").startswith(kind[:4])]
    items = []
    for e in entries:
        # selecting an entry plays it (daemon owns queue/resume)
        eid = e.get("id")
        items.append((e.get("title", "?"),
                      (lambda i=eid: boxapi.post("/play", {"entry": i}))))
    return Menu(kind.title(), lambda: items or [("(empty)", None)])


def _settings_menu():
    def toggle_output():
        cur = (boxapi.get("/output", timeout=2.0) or {}).get("output", "bt")
        nxt = "local" if cur == "bt" else "bt"   # jack <-> BT (RESEARCH §3)
        boxapi.post("/output", {"output": nxt})
    return Menu("Settings", lambda: [
        ("Audio out: jack/BT", toggle_output),
        ("Shutdown", lambda: boxapi.post("/shutdown", {})),
    ])


# --- transport (same routing as pi/buttons.py) -----------------------------

def transport(action):
    try:
        boxapi.post("/" + action, {})
        log(f"{action} -> daemon")
    except OSError as e:
        log(f"{action}: {e}")


# --- render (stub — wire an ST7789 driver here) ----------------------------

def render(stack):
    """Draw the current menu. Replace the PNG stub with an ST7789 push.
    Reuse ui.py's album-art cache + marquee for Now Playing."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    menu = stack[-1]
    img = Image.new("RGB", (W, H), (0, 0, 0) if not locked() else (8, 8, 8))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, W, 22), fill=(30, 30, 40))
    d.text((6, 5), menu.title, fill=(255, 255, 255))
    pct = battery_pct()
    if pct is not None:
        d.text((W - 44, 5), f"{pct:.0f}%", fill=(200, 200, 200))
    if locked():
        d.text((W - 90, 5), "HOLD", fill=(200, 120, 120))
    top = max(0, menu.sel - 4)
    for row, (label, _) in enumerate(menu.items[top:top + 8]):
        i = top + row
        y = 26 + row * 26
        if i == menu.sel:
            d.rectangle((0, y, W, y + 24), fill=(40, 80, 160))
        d.text((10, y + 4), str(label)[:34], fill=(255, 255, 255))
    if PNG_PATH:
        img.save(PNG_PATH)
    # else: push `img` to the ST7789 over SPI here.


# --- main loop -------------------------------------------------------------

def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", UDP_PORT))
    sock.settimeout(0.2)

    home = Menu("pipod", _home_items)
    home.load()
    stack = [home]
    last_pos = None
    last_scroll_t = 0.0
    log("started — waiting for wheel events on udp:9090")

    while True:
        render(stack)
        try:
            data, _ = sock.recvfrom(8)
        except socket.timeout:
            continue
        if len(data) != 3 or locked():
            continue
        idx, state, pos = data[0], data[1], data[2]

        if idx == SCROLL:
            if last_pos is not None:
                raw = pos - last_pos
                if raw > WHEEL_MAX // 2:      # wrap around the ring
                    raw -= WHEEL_MAX
                elif raw < -WHEEL_MAX // 2:
                    raw += WHEEL_MAX
                now = time.time()
                fast = (now - last_scroll_t) * 1000 < SCROLL_ACCEL_MS
                step = (2 if fast else 1) * (1 if raw > 0 else -1) if raw else 0
                if step:
                    stack[-1].move(step)
                last_scroll_t = now
            last_pos = pos
            continue

        if state != 1:        # act on press only
            continue

        if idx == BTN_MENU:
            if len(stack) > 1:
                stack.pop()
        elif idx == BTN_CENTER:
            _, action = (stack[-1].items[stack[-1].sel]
                         if stack[-1].items else (None, None))
            if callable(action):
                res = action()
                if isinstance(res, Menu):     # submenu
                    res.load()
                    stack.append(res)
            elif action == "now":
                pass                          # TODO: push a Now Playing view
        elif idx == BTN_PLAY:
            transport("playpause")
        elif idx == BTN_PREV:
            transport("prev")
        elif idx == BTN_NEXT:
            transport("next")


if __name__ == "__main__":
    main()
