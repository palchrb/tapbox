#!/usr/bin/env python3
"""Gate the wifi_ps_bt_off setting (PWA toggle, default OFF).

When enabled, the wifi-PS governor holds power save OFF during any BT audio
session — even fully cached playback — so the WLAN core stops its beacon-wake
coex re-arbitrations against the A2DP stream (the suspected BCM43430 crash
trigger). Costs ~15-20%% listening runtime, so it must be the parent's
explicit choice: default 0, and with it off the governor behaves exactly as
before."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import sysinfo  # noqa: E402

# 1. the setting exists, defaults OFF, and is bounded 0..1
d, lo, hi = sysinfo.SETTING_SPECS["wifi_ps_bt_off"]
assert (d, lo, hi) == (0, 0, 1), (d, lo, hi)
print("1. wifi_ps_bt_off setting exists, default OFF OK")

SETTINGS = {"wifi_ps_bt_off": 0}
daemon.load_settings = lambda: SETTINGS
daemon._bt_playback_active = lambda: True
daemon._streaming_now = lambda: False  # cached/local playback

# 2. setting OFF -> cached BT playback keeps today's verdict (PS on)
assert daemon._ps_want_off() is False, \
    "with the setting off the governor must behave exactly as before"
print("2. setting OFF -> unchanged behavior (PS on for cached play) OK")

# 3. setting ON + BT audio session -> PS off
SETTINGS["wifi_ps_bt_off"] = 1
assert daemon._ps_want_off() is True, \
    "setting on + BT session must hold PS off"
print("3. setting ON + BT session -> PS off OK")

# 4. setting ON but no BT session -> idle verdict stands (PS on)
daemon._bt_playback_active = lambda: False
assert daemon._ps_want_off() is False
print("4. setting ON + no BT session -> PS on (battery unaffected idle) OK")

# 5. streaming verdicts pass through untouched (True and the None hold)
daemon._streaming_now = lambda: True
assert daemon._ps_want_off() is True
daemon._streaming_now = lambda: None
assert daemon._ps_want_off() is None, "the None hold must survive"
print("5. streaming True / unknown None pass through untouched OK")

print("\nall wifi_ps_bt_setting checks passed")
