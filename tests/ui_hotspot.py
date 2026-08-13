#!/usr/bin/env python3
"""Gate the screen's 'Setup hotspot' settings row: the only way into a
box at a new place with no known wifi around is an AP started from the
BOX (the PWA needs a shared network — exactly what's missing), so the
settings menu must offer it. Also guards the row-index shift: Shut down
and Restart moved to 9/10, and an inverted mapping there is the exact
field-reported bug (Restart powered the box off) that must never come
back."""
import os
import sys
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")  # no SPI in tests

import ui  # noqa: E402

# the hotspot handler sleeps so the parent can read the SSID/password —
# shim ui's time so the test doesn't (monotonic stays real)
ui.time = types.SimpleNamespace(monotonic=time.monotonic,
                                time=time.time, sleep=lambda s: None)

GETS, POSTS, PUTS, MSGS = [], [], [], []


def fake_get(path, timeout=10):
    GETS.append(path)
    return {"configured": None, "devices": []}


def fake_post(path, body=None, timeout=15):
    POSTS.append((path, body))
    if path == "/wifi/hotspot" and body.get("enabled"):
        return {"ok": True, "ssid": "Vibb-zero2", "password": "vibb123"}
    return {"ok": True}


ui.api_get = fake_get
ui.api_post = fake_post
ui.api_put = lambda path, body, timeout=10: PUTS.append((path, body)) or {}

app = object.__new__(ui.App)
app.view = "settings"
app.stack = []
app.sel = 0
app.settings = {"screen_timeout_s": 30, "screen_brightness": 100,
                "volume_cap": 100, "idle_shutdown_min": 30, "simple_nav": 0}
app.system = {"wifi": {"enabled": True, "hotspot": False}}
app.bt = {"devices": []}
app.status = {}
app.last_system = 1e18
app.dirty = False
app.draw_message = lambda msg: MSGS.append(msg)
app.confirm = lambda: True

# 1. the settings menu has the row, right after Wi-Fi
items = app.current_items()
labels = [i[0] if isinstance(i, tuple) else i for i in items]
# Positions are NOT pinned: dispatch is label-based precisely so rows
# can be inserted (the index form once made "Restart" power the box
# off). Pin membership and the ordering that matters to the user.
assert "Setup hotspot" in labels and "Wi-Fi" in labels, labels
assert labels.index("Setup hotspot") == labels.index("Wi-Fi") + 1, labels
assert {"Shut down", "Restart"} <= set(labels), labels
print("1. settings menu: Setup hotspot row after Wi-Fi OK")

# 2. selecting it starts the AP and shows what to join
app.sel = labels.index("Setup hotspot")
app.select_setting()
assert POSTS == [("/wifi/hotspot", {"enabled": True})], POSTS
assert any("Vibb-zero2" in m and "vibb123" in m for m in MSGS), MSGS
assert app.last_system == 0.0, "state row must refresh after the toggle"
print("2. select -> starts the hotspot, shows SSID + password OK")

# 3. selecting again while active stops it
POSTS.clear()
app.system = {"wifi": {"enabled": True, "hotspot": True}}
assert app.current_items()[6][1] == "on", "active hotspot must show 'on'"
app.select_setting()
assert POSTS == [("/wifi/hotspot", {"enabled": False})], POSTS
print("3. select while active -> stops the hotspot OK")

# 4. the other rows still map right, wherever they sit
POSTS.clear()
app.sel = labels.index("Bluetooth")
app.select_setting()
assert GETS[-1] == "/bt" and app.view == "bt", (GETS, app.view)
app.view, app.stack = "settings", []
app.sel = labels.index("Storage")
app.select_setting()
assert app.view == "storage", app.view
app.view, app.stack = "settings", []
print("4. Bluetooth and Storage rows still work OK")

# 5. Shut down powers off, Restart restarts — NOT inverted. This is the
# exact field-reported bug that index-based dispatch caused, so it stays
# pinned even though the dispatch is label-based now.
POSTS.clear()
app.sel = labels.index("Shut down")
app.select_setting()
assert POSTS == [("/system/shutdown", {"restart": False})], POSTS
POSTS.clear()
app.sel = labels.index("Restart")
app.select_setting()
assert POSTS == [("/system/shutdown", {"restart": True})], POSTS
print("5. Shut down/Restart map correctly (not inverted) OK")

print("ui_hotspot: all OK")
