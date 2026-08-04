#!/usr/bin/env python3
"""Gate that only AUDIO devices are offered as speakers.

Field 2026-08-04: a paired Nintendo Pro Controller appeared in the
PWA's "Bluetooth speaker" list as a connectable device. One tap would
have written a gamepad's MAC into the configured-output file and
rewritten asound.conf to route A2DP at it — silence, and a confusing
repair job. bt_status now carries an `audio` flag per device and the
PWA renders non-audio bonds without a Connect button (they stay
listed, so they can still be renamed and unpaired)."""
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["TAPBOX_ASOUND"] = os.path.join(TMP, "asound.conf")
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import bt, btbus  # noqa: E402

# 1. the flag reaches the API, per device
btbus.paired_devices = lambda: [
    {"mac": "AA:BB:CC:DD:EE:FF", "name": "JBL GO", "audio": True},
    {"mac": "11:22:33:44:55:66", "name": "Pro Controller", "audio": False},
]
btbus.connected_devices = lambda: [
    {"mac": "11:22:33:44:55:66", "name": "Pro Controller", "audio": False}]
st = bt.bt_status()
by_mac = {d["mac"]: d for d in st["devices"]}
assert by_mac["AA:BB:CC:DD:EE:FF"]["audio"] is True
assert by_mac["11:22:33:44:55:66"]["audio"] is False
assert by_mac["11:22:33:44:55:66"]["connected"] is True, \
    "a controller still shows as connected — it just is not a speaker"
print("1. bt_status carries the audio flag per device OK")

# 2. an older backend that omits the flag must stay CONNECTABLE — a
#    missing flag means 'unknown', and hiding every speaker would be a
#    far worse failure than showing one gamepad
btbus.paired_devices = lambda: [{"mac": "AA:BB:CC:DD:EE:FF", "name": "Old"}]
btbus.connected_devices = lambda: []
assert bt.bt_status()["devices"][0]["audio"] is True, \
    "unknown must default to speaker, not hide the device"
print("2. a backend without the flag defaults to speaker OK")

# 3. the PWA honors it: Connect is built only for speakers, and the
#    row itself is always appended (rename/forget must stay reachable)
src = open(os.path.join(REPO, "pi", "web", "app.js"),
           encoding="utf-8", errors="surrogateescape").read()
assert "const isSpeaker = d.audio !== false;" in src, \
    "the PWA must read the flag"
assert re.search(r"if \(isSpeaker\) \{[^}]*?/bt/connect", src, re.S), \
    "the Connect button must be gated on isSpeaker"
assert "not a speaker" in src, "non-audio rows must say so"
# forget/rename stay outside the gate
gate = src[src.index("if (isSpeaker) {"):]
assert "/bt/forget" in gate and "/bt/rename" in gate, \
    "unpair and rename must remain available for every device"
print("3. PWA: Connect gated on the flag, forget/rename kept OK")

print("\nall bt_speaker_only checks passed")
