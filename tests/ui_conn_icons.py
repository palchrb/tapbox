#!/usr/bin/env python3
"""Gate the wifi/BT connection icons drawn next to the battery. They
must render without error for every state, be GOOD-coloured only when
connected (DIM + a slash otherwise), and the BT glyph must appear ONLY
when a speaker is configured (bt_ready present in /system) — a box with
no speaker shows no BT icon at all. Motivated by the 40s post-boot
confusion (log 2026-07-20): wifi up, speaker off, nothing playing, and
the screen gave no at-a-glance reason."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")

from PIL import Image, ImageDraw  # noqa: E402

import ui  # noqa: E402


def strip(system):
    """Draw the battery corner onto a blank tile; return (colours, lit)
    for the icon strip left of the battery (x < W-34): the set of
    distinct non-background colours, and how many pixels are lit."""
    img = Image.new("RGB", (ui.W, 28), ui.BG)
    ui.battery_corner(ImageDraw.Draw(img), system)
    cols, lit = set(), 0
    for x in range(0, ui.W - 34):
        for y in range(0, 24):
            p = img.getpixel((x, y))
            if p != ui.BG:
                cols.add(p)
                lit += 1
    return cols, lit


def pixels(system):
    return strip(system)[0]


# 1. never raises for any state, including empty/missing system
for s in ({}, None, {"wifi": {}}, {"wifi": {"ip": "10.0.0.5"}},
          {"wifi": {"ip": "1.2.3.4"}, "bt_ready": True},
          {"wifi": {"hotspot": True, "ip": "10.42.0.1"}}):
    pixels(s)
print("1. renders for every state without error OK")

# 2. wifi connected -> the GOOD colour appears in the strip
cols = pixels({"wifi": {"ip": "10.0.0.5"}})
assert ui.GOOD in cols, "connected wifi must draw the GOOD colour"
print("2. connected wifi draws green OK")

# 3. wifi down -> no GOOD colour, but DIM is present (the struck glyph)
cols = pixels({"wifi": {}})
assert ui.GOOD not in cols and ui.DIM in cols
print("3. disconnected wifi draws dim + slash, never green OK")

# 4. hotspot mode is NOT 'connected to a network' -> not green
cols = pixels({"wifi": {"hotspot": True, "ip": "10.42.0.1"}})
assert ui.GOOD not in cols, "hotspot must not read as a live uplink"
print("4. hotspot is not shown as a connected uplink OK")

# 5. BT icon only when a speaker is configured. The no-bt-key strip has
# strictly fewer lit pixels than the same state WITH a bt_ready key —
# proving the BT glyph is genuinely absent, not just recoloured. wifi
# is left DOWN here so any GOOD pixel can only come from the BT glyph.
_, lit_no_bt = strip({"wifi": {}})
_, lit_off = strip({"wifi": {}, "bt_ready": False})
cols_on, _ = strip({"wifi": {}, "bt_ready": True})
cols_off, _ = strip({"wifi": {}, "bt_ready": False})
assert lit_off > lit_no_bt, "a configured speaker must add a BT glyph"
assert ui.GOOD in cols_on, "connected speaker must draw green BT"
assert ui.GOOD not in cols_off, "an off speaker must never draw green"
print("5. BT glyph appears only when a speaker is configured OK")

# 6. the exact case from the field log: wifi up, speaker configured but
# off -> wifi green, BT dim. Both colours coexist.
cols = pixels({"wifi": {"ip": "192.168.0.151"}, "bt_ready": False})
assert ui.GOOD in cols and ui.DIM in cols
print("6. wifi-up + speaker-off shows green wifi and dim BT together OK")

# 7. FAST PATH: a /status update (polled every 1-2s) folds its live
# bt_connected into self.system (which the icon reads) so the BT icon
# tracks connect/drop as fast as the popup, not the 30s /system poll.
stub = type("S", (), {"status": {}, "system": {}, "dirty": False,
                       "_pp_expect": None, "_pos_expect": None,
                       "vol_mode_until": 0.0, "volume_flash": 0.0,
                       "_expect_target": None})()
ui.App._set(stub, "status", {"bt_connected": True, "playing": False})
assert stub.system.get("bt_ready") is True, stub.system
ui.App._set(stub, "status", {"bt_connected": False, "playing": False})
assert stub.system.get("bt_ready") is False, stub.system
# a /status with no bt_connected key (no speaker configured) must not
# invent one — else a built-in-only box would sprout a BT icon
stub2 = type("S", (), {"status": {}, "system": {}, "dirty": False,
                       "_pp_expect": None, "_pos_expect": None,
                       "vol_mode_until": 0.0, "volume_flash": 0.0,
                       "_expect_target": None})()
ui.App._set(stub2, "status", {"playing": True})
assert "bt_ready" not in stub2.system, stub2.system
print("7. /status folds live bt_connected into the icon (fast, gated) OK")

print("UI CONN ICONS OK — connection state is legible on every view, "
      "BT only shows when a speaker is configured, and it updates as "
      "fast as the popup.")
