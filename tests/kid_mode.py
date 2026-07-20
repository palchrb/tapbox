#!/usr/bin/env python3
"""Gate kid mode (simple_nav): the flat big-cover carousel. B/Y flip
through every entry across sections, A plays the tile (or pauses the one
already playing), the mode setting validates, and flipping it swaps the
view both ways without touching an open settings screen."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")

import ui  # noqa: E402
from tapbox import sysinfo  # noqa: E402

# 1. the setting exists, defaults off, now a 3-way (0 menus/1 flat/2 cats)
assert sysinfo.SETTING_SPECS["simple_nav"] == (0, 0, 2)
print("1. simple_nav setting registered (default off, 3-way) OK")


class FakeDisplay:
    on = True
    def show(self, img): pass
    def set_backlight(self, on): pass
    def set_brightness(self, b): pass


app = ui.App(FakeDisplay(), None)
app._lib_at = time.monotonic() + 999
app.library = {"sections": [
    {"name": "Musikk", "entries": [
        {"id": "m1", "name": "Sanger", "target": "spotify:playlist:a"}]},
    {"name": "Fortellinger", "entries": [
        {"id": "f1", "name": "Fanto", "target": "https://radio.nrk.no/podkast/x"},
        {"id": "f2", "name": "Bablo", "target": "https://ex.com/feed.rss"}]}]}

# 2. the carousel is FLAT: all entries across sections, library order
assert [e["id"] for e in app.flat_entries()] == ["m1", "f1", "f2"]
print("2. carousel flattens every section in order OK")

# 3. flipping the setting swaps the view both ways...
app.settings = {"simple_nav": 1}
app.view = "home"
app._apply_nav_mode()
assert app.view == "carousel" and app.stack == []
app.settings = {"simple_nav": 0}
app._apply_nav_mode()
assert app.view == "home"
# ...but never yanks an open settings screen
app.settings = {"simple_nav": 1}
app.view = "settings"
app._apply_nav_mode()
assert app.view == "settings", "settings view must be left alone"
print("3. mode flip swaps home<->carousel, leaves settings alone OK")

# 4. buttons: Y forward (wraps), B back, A plays the tile AND opens the
# normal now-playing view (kept as-is per field feedback)
posts = []
ui.api_post = lambda path, body=None, timeout=15: (posts.append((path, body)), {})[1]
app.settings = {"simple_nav": 1}
app.view, app.car_sel, app.status = "carousel", 0, {}
app.handle_carousel("y")
app.handle_carousel("y")
assert app.car_sel == 2
app.handle_carousel("y")
assert app.car_sel == 0, "must wrap around"
app.handle_carousel("b")
assert app.car_sel == 2, "B must go backwards (wrapping)"
app.handle_carousel("a")
assert posts == [("/play", {"id": "f2"})], posts
assert app.view == "now", "A must open now-playing"
assert app.stack and app.stack[-1][0] == "carousel", \
    "back from now-playing must return to the carousel"
print("4. Y/B flip with wrap, A plays + opens now-playing OK")

# 5. A has ONE meaning — "play this tile" — also when it's already the
# playing target (the daemon turns a same-target replay into a plain
# unpause/no-op, so nothing restarts)
posts.clear()
app.view, app.stack = "carousel", []
app.status = {"target": "https://ex.com/feed.rss", "playing": True}
app.handle_carousel("a")
assert posts == [("/play", {"id": "f2"})], posts
assert app.view == "now"
print("5. A always means play-this-tile (daemon absorbs replays) OK")

# 6. hold-B in now-playing returns to the carousel, on the playing tile
app.settings = {"simple_nav": 1}
app.view = "now"
app._back_to_episodes()
assert app.view == "carousel" and app.stack == []
assert app.car_sel == 2, "should land on the playing tile"
print("6. hold-B in now-playing lands on the playing carousel tile OK")

print("KID MODE OK — carousel browses, now-playing plays, hold-B returns.")
