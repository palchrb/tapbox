#!/usr/bin/env python3
"""Gate nav mode 2 (category carousel): the box screen shows a carousel
of CATEGORIES first (only non-empty ones), A opens that category's cover
carousel, hold-B steps back up to the categories, and hold-B in
now-playing lands on the playing entry's category carousel. Mode 0 (text
menus) and mode 1 (flat carousel) are unaffected — those live in
kid_mode.py."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")

import ui  # noqa: E402


class FakeDisplay:
    on = True

    def show(self, img):
        pass

    def set_backlight(self, on):
        pass

    def set_brightness(self, b):
        pass


app = ui.App(FakeDisplay(), None)
app._lib_at = time.monotonic() + 999
app.settings = {"simple_nav": 2}
app.library = {"sections": [
    {"id": "musikk", "name": "Musikk", "entries": [
        {"id": "m1", "name": "Sanger", "target": "spotify:playlist:a"}]},
    {"id": "empty", "name": "Tom", "entries": []},   # must be hidden
    {"id": "fort", "name": "Fortellinger", "entries": [
        {"id": "f1", "name": "Fanto", "target": "https://radio.nrk.no/p/x"},
        {"id": "f2", "name": "Bablo", "target": "https://ex.com/feed.rss"}]}]}

# 1. the category carousel hides empty categories
assert [s["id"] for s in app.carousel_cats()] == ["musikk", "fort"], \
    [s["id"] for s in app.carousel_cats()]
print("1. category carousel lists only non-empty categories OK")

# 2. entering the root: mode 2's browse root is 'cats'
app.view = "home"
app._apply_nav_mode()
assert app.view == "cats" and app.car_section is None
print("2. mode 2 browse root is the category carousel OK")

# 3. A on a category opens ITS entry carousel (scoped to that section)
app.cat_sel = 1  # "Fortellinger"
app.handle_cats("a")
assert app.view == "carousel" and app.car_section == "fort"
assert [e["id"] for e in app.carousel_entries()] == ["f1", "f2"], \
    "scoped carousel must show only that category's entries"
print("3. A opens the category's own scoped cover carousel OK")

# 4. flipping inside the category stays within it; A plays the tile
posts = []
ui.api_post = lambda p, body=None, timeout=15: (posts.append((p, body)), {})[1]
app.status = {}
app.handle_carousel("y")  # f1 -> f2
assert app.car_sel == 1
app.handle_carousel("a")
assert posts == [("/play", {"id": "f2"})], posts
assert app.view == "now"
print("4. inside a category: flip + play works, scoped OK")

# 5. hold-B inside a category steps back UP to the categories (short B
# still flips tiles)
app.view, app.car_section, app.car_sel = "carousel", "fort", 1
app.status = {}
app.handle_carousel("b")           # short B: flip, stays in the category
assert app.view == "carousel" and app.car_section == "fort"
app.handle_carousel("b_long")      # hold-B: up to the categories
assert app.view == "cats" and app.car_section is None
print("5. hold-B leaves a category for the category carousel OK")

# 6. hold-B in now-playing lands on the PLAYING entry's category carousel
app.view = "now"
app.car_section = None
app.status = {"target": "https://ex.com/feed.rss"}  # f2, in 'fort'
app._back_to_episodes()
assert app.view == "carousel" and app.car_section == "fort", \
    (app.view, app.car_section)
assert app.car_sel == 1, "should land on the playing tile within its category"
print("6. hold-B in now-playing opens the playing entry's category OK")

# 7. a category that emptied out under us: hold-B still escapes to cats
app.view, app.car_section = "carousel", "empty"
app.car_sel = 0
app.handle_carousel("b_long")
assert app.view == "cats" and app.car_section is None
print("7. an emptied category still lets hold-B escape to categories OK")

# 8. switching mode 2 -> 0 from the category carousel drops to text menus;
# switching -> 1 drops to the flat carousel (an open settings is spared)
app.view, app.car_section = "carousel", "fort"
app.settings = {"simple_nav": 0}
app._apply_nav_mode()
assert app.view == "home" and app.car_section is None
app.view = "cats"
app.settings = {"simple_nav": 1}
app._apply_nav_mode()
assert app.view == "carousel"
app.view = "settings"
app.settings = {"simple_nav": 2}
app._apply_nav_mode()
assert app.view == "settings", "an open settings screen is never yanked"
print("8. mode switches reconcile the browse root, spare settings OK")

print("CAT CAROUSEL OK — categories first, scoped entry carousels, "
      "hold-B walks back up, empties hidden.")
