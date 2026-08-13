#!/usr/bin/env python3
"""Gate the wifi-probe protections: with wifi auto-off'd (no known
network) the watchdog probes every ~10 min by unblocking the radio and
letting NM scan — but the Zero 2 W's wifi and BT share one 2.4GHz
radio, so a scan mid-A2DP stutters the audio and is the documented
firmware-crash trigger.

Two layers, both gated here:
 - probe hold: while an mpv session exists on the bt output (PAUSED
   COUNTS — a kid mid-listen resumes any second, straight into a live
   ~30s probe window) the probe is DEFERRED, not skipped: it fires on
   the first pass after the session ends.
 - wifi_probe setting (PWA): 0 = never probe at all; the radio stays
   down until the explicit reconnect button (set_wifi).

Runs the REAL _wifi_watchdog thread against fakes with a scaled clock —
no hardware needed."""
import json
import os
import sys
import tempfile
import time
import threading
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ["VIBB_SETTINGS"] = os.path.join(STATE, "settings.json")
os.environ.setdefault("VIBB_CACHE", tempfile.mkdtemp())
os.environ["VIBB_WIFI_WATCHDOG_DELAY"] = "0"
os.environ["VIBB_WIFI_PROBE_INTERVAL"] = "1"
os.environ["VIBB_WIFI_PROBE_WINDOW"] = "1"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import netmgmt  # noqa: E402


def wait_for(what, pred, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise SystemExit(f"TIMEOUT waiting for: {what}")


def set_setting(key, value):
    with open(os.environ["VIBB_SETTINGS"], "w") as f:
        json.dump({key: value}, f)


# 1. vibbd wires its playback check into netmgmt at import
assert netmgmt.probe_hold[0] is daemon._bt_playback_active, \
    "daemon must install _bt_playback_active as the probe hold"
print("1. vibbd installs the probe-hold hook OK")


# --- _bt_playback_active's verdicts (direct calls, no threads) --------------

ALIVE = [True]
daemon.Orchestrator._mpv_alive = lambda self: ALIVE[0]


def set_output(device):
    with open(daemon.OUT_FILE, "w") as f:
        json.dump({"output": device, "pcm": "x"}, f)


set_output("bt")
assert daemon._bt_playback_active() is True, "mpv session over bt must hold"
ALIVE[0] = False
assert daemon._bt_playback_active() is False, "no player -> no hold"
ALIVE[0] = True
set_output("local")
assert daemon._bt_playback_active() is False, \
    "local output doesn't touch the radio"
print("2. _bt_playback_active: any mpv session on the bt output holds "
      "(pause included) OK")


# --- the real watchdog thread against fakes ---------------------------------

# scaled clock: the loop's sleep(30)/sleep(5) become 0.3s/0.05s; only
# netmgmt sees the shim — the rest of the process keeps real time
netmgmt.time = types.SimpleNamespace(
    monotonic=time.monotonic, time=time.time,
    sleep=lambda s: time.sleep(s / 100.0))

RF = []
NET = {"enabled": False, "ssid": None}


def fake_rfkill(enabled):
    RF.append(enabled)
    # unblocking lets NM find the known network again (the happy probe)
    NET.update(enabled=enabled, ssid="homenet" if enabled else None)


netmgmt._link_up = lambda: False
netmgmt._rfkill = fake_rfkill
netmgmt.wifi_state = lambda: (NET["enabled"], NET["ssid"], None)

HOLD = [True]
netmgmt.probe_hold[0] = lambda: HOLD[0]
netmgmt._auto.update(blocked=True, next_probe=0.0, probe_held=False)
threading.Thread(target=netmgmt._wifi_watchdog, daemon=True).start()

# 3. music playing: the probe is held, the radio is never unblocked
wait_for("watchdog notices the hold", lambda: netmgmt._auto["probe_held"])
time.sleep(1.0)  # several loop passes worth
assert RF == [], f"radio was touched during playback: {RF}"
assert netmgmt._auto["blocked"], "auto-off state must survive the hold"
print("3. probe held while music plays — radio untouched OK")

# 4. music stops: the deferred probe fires on the next pass and reconnects
HOLD[0] = False
wait_for("deferred probe reconnects", lambda: not netmgmt._auto["blocked"])
assert RF and RF[0] is True, f"probe never unblocked the radio: {RF}"
assert not netmgmt._auto["probe_held"], "hold flag must clear on probe"
print("4. probe fires right after the music stops and reconnects OK")

# 5. wifi_probe=0 (PWA): never probe, even with no music playing
set_setting("wifi_probe", 0)
NET.update(enabled=False, ssid=None)
netmgmt._auto.update(blocked=True, next_probe=0.0, probe_held=False)
seen = len(RF)
time.sleep(1.5)  # several loop passes worth
assert len(RF) == seen, f"probing-disabled still touched the radio: {RF}"
assert netmgmt._auto["blocked"], "must stay off until a manual reconnect"
print("5. wifi_probe=0 never touches the radio OK")

# 6. flipping the setting back on resumes probing (settings re-read live)
set_setting("wifi_probe", 1)
wait_for("probe resumes after re-enable",
         lambda: not netmgmt._auto["blocked"])
assert RF[seen] is True, f"probe did not unblock the radio: {RF}"
print("6. wifi_probe=1 resumes probing live OK")


# --- the net-changed hook: a wifi SWITCH while online must restart
# --- go-librespot (stale AP/dealer connections wedged its API and froze
# --- the UI — field log 2026-07-17) ----------------------------------------

assert netmgmt.net_changed[0] is daemon._net_changed, \
    "daemon must install the net-changed hook"
CHANGED = []
netmgmt.net_changed[0] = lambda: CHANGED.append(1)
netmgmt.hotspot_active = lambda: False
netmgmt._known_wifi_names = lambda: {"TP-LINK_4390"}

# 7. successful join fires the hook exactly once
netmgmt._nmcli = lambda *a, timeout=60: (0, "Connection activated")
r = netmgmt.wifi_connect("TP-LINK_4390")
assert r["ok"] and CHANGED == [1], (r, CHANGED)
print("7. wifi switch success fires the go-librespot restart hook OK")

# 8. a failed join must NOT restart anything
CHANGED.clear()
netmgmt._nmcli = lambda *a, timeout=60: (4, "Secrets were required")
r = netmgmt.wifi_connect("TP-LINK_4390")
assert not r["ok"] and CHANGED == [], (r, CHANGED)
print("8. failed join leaves go-librespot alone OK")

print("wifi_probe_hold: all OK")
