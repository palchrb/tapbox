#!/usr/bin/env python3
"""Gate the stall watchdog's zombie-transport detection: a BT link can
die while bluez keeps saying connected — mpv's position ticks on, the
A2DP PCM stays listed, but nothing leaves the radio. The controller's
TX byte counter is the ground truth, and a flat counter across STALL_S
of claimed playback must tear the link down (bt.py reconnect) before
restarting the player; ensure() would no-op against the lying state.

Also gates: the frozen-position stall still restarts (without touching
the radio when the output is ready), pause/local-output/no-adapter
never trigger, a TX counter wrap doesn't false-positive, and the poll
cadence honors TAPBOX_STALL_POLL. Runs the REAL watchdog thread against
fakes — no hardware needed."""
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = STATE
os.environ["TAPBOX_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ.setdefault("TAPBOX_CACHE", tempfile.mkdtemp())
os.environ["TAPBOX_BT_FILE"] = os.path.join(STATE, "bt-headset")
os.environ["TAPBOX_BT_LOCKFILE"] = os.path.join(STATE, "bt.lock")
# test timescale: stall after 0.4s, sampled every 0.05s
os.environ["TAPBOX_STALL_S"] = "0.4"
os.environ["TAPBOX_STALL_POLL"] = "0.05"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from tapbox import bt as bt_mod  # noqa: E402

MAC = "AA:BB:CC:DD:EE:FF"


def wait_for(what, pred, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise SystemExit(f"TIMEOUT waiting for: {what}")


# --- unit: hci_tx_bytes parsing (patch the runner, no radio) ---------------

HCICONFIG_OUT = """hci0:\tType: Primary  Bus: UART
\tBD Address: B8:27:EB:00:00:00  ACL MTU: 1021:8  SCO MTU: 64:1
\tUP RUNNING
\tRX bytes:12345 acl:6 sco:0 events:100 errors:0
\tTX bytes:987654 acl:12 sco:0 commands:50 errors:0
"""

_orig_run = bt_mod._run
bt_mod._run = lambda *a, **k: (0, HCICONFIG_OUT)
assert bt_mod.hci_tx_bytes() == 987654, "TX counter not parsed"
bt_mod._run = lambda *a, **k: (127, "")  # hciconfig missing
assert bt_mod.hci_tx_bytes() is None, "missing hciconfig must give None"
bt_mod._run = lambda *a, **k: (0, "hci0: no counters here")
assert bt_mod.hci_tx_bytes() is None, "unparsable output must give None"
bt_mod._run = _orig_run
print("1. hci_tx_bytes parses the counter, None when unavailable OK")


# --- unit: reconnect = disconnect THEN connect (the zombie cure) -----------

CALLS = []
bt_mod.btbus.disconnect_device = (
    lambda mac: (CALLS.append(("disconnect", mac)), (True, ""))[1])
bt_mod.connect = lambda mac: CALLS.append(("connect", mac)) or True
bt_mod.ensure = lambda: CALLS.append(("ensure", None)) or True


class TimeShim:
    """Skip bt.py's teardown settle delay — scoped to the bt module's
    own `time` attribute. Mutating the shared time module's sleep while
    daemon's arbiter/stall threads are live made them spin and steal
    this file's scripted watchdog ticks (QA review Q2, a real flake)."""
    sleep = staticmethod(lambda s: None)

    def __getattr__(self, name):
        return getattr(time, name)


bt_mod.time = TimeShim()

with open(bt_mod.MAC_FILE, "w") as f:
    f.write(MAC + "\n")
assert bt_mod.reconnect() is True
assert CALLS == [("disconnect", MAC), ("connect", MAC)], CALLS
print("2. reconnect tears the link down before rebuilding it OK")

CALLS.clear()
os.remove(bt_mod.MAC_FILE)
assert bt_mod.reconnect() is True
assert CALLS == [("ensure", None)], CALLS
print("3. reconnect without a configured device falls back to ensure OK")
bt_mod.time = time


# --- the watchdog thread against fakes -------------------------------------

ALIVE = [False]
STOPPED, SPAWNED, RECOVERED = [], [], []
daemon.Orchestrator._mpv_alive = lambda self: ALIVE[0]
daemon.Orchestrator._stop_child = (
    lambda self: (STOPPED.append(1), ALIVE.__setitem__(0, False)))
daemon.Orchestrator._spawn = (
    lambda self, target, **kw: SPAWNED.append(target))
daemon._bt_recover = lambda verb: RECOVERED.append(verb)
daemon._audio_ready = lambda: True

MPV = {"pause": False, "pos": 0.0, "tick": 0.0}
TX = {"value": 1000, "step": 0}


def fake_mpv_get(prop):
    if prop == "pause":
        return MPV["pause"]
    MPV["pos"] += MPV["tick"]
    return MPV["pos"]


def fake_tx():
    v = TX["value"]
    if v is not None:
        TX["value"] = v + TX["step"]
    return v


daemon.mpv_get = fake_mpv_get
daemon._bt.hci_tx_bytes = fake_tx


def set_output(device):
    with open(daemon.OUT_FILE, "w") as f:
        json.dump({"output": device, "pcm": "x"}, f)


def start_playing():
    daemon.ORCH.target = "https://feeds.example.com/show"
    daemon.ORCH.source = "mpv"
    daemon.ORCH.child_started = time.monotonic() - 60  # past startup grace
    ALIVE[0] = True


def reset():
    ALIVE[0] = False
    time.sleep(0.3)  # a few idle polls clear the watchdog's trackers
    STOPPED.clear()
    SPAWNED.clear()
    RECOVERED.clear()
    MPV.update(pause=False, pos=0.0, tick=0.0)
    TX.update(value=1000, step=0)


# 4. zombie: position ticks, TX flat, output=bt -> reconnect + respawn
set_output("bt")
MPV["tick"] = 1.0
start_playing()
wait_for("zombie stall -> respawn", lambda: SPAWNED)
assert STOPPED, "player was never stopped"
assert RECOVERED == ["reconnect"], (
    f"zombie must rebuild the link (got {RECOVERED})")
print("4. flat TX while playing rebuilds the BT link and restarts OK")

# 5. healthy playback: position ticks AND TX moves -> untouched
reset()
MPV["tick"] = 1.0
TX["step"] = 1750  # ~35kB/s at the test poll rate
start_playing()
time.sleep(1.5)
assert not STOPPED and not RECOVERED, "healthy playback was disturbed"
print("5. moving TX counter never triggers OK")

# 6. frozen position still stalls; ready output -> no radio surgery
reset()
MPV["tick"] = 0.0
TX["step"] = 1750
start_playing()
wait_for("frozen-position stall -> respawn", lambda: SPAWNED)
assert RECOVERED == [], "ready output must not trigger BT recovery"
print("6. frozen position restarts without touching the radio OK")

# 7. paused: TX legitimately flat -> never a stall
reset()
MPV.update(pause=True, tick=0.0)
start_playing()
time.sleep(1.5)
assert not STOPPED, "pause was treated as a stall"
print("7. paused playback never stalls OK")

# 8. local output: radio TX is irrelevant there
reset()
set_output("local")
MPV["tick"] = 1.0
start_playing()
time.sleep(1.5)
assert not STOPPED, "local output must ignore the TX counter"
print("8. flat TX on the local output is ignored OK")

# 9. no adapter (hci_tx_bytes -> None): can't judge, never trigger
reset()
set_output("bt")
MPV["tick"] = 1.0
TX["value"] = None
start_playing()
time.sleep(1.5)
assert not STOPPED, "unknown TX state must not trigger"
print("9. missing TX counter never triggers OK")

# 10. counter reset/wrap (value decreases) restarts the clock, no stall
reset()
MPV["tick"] = 1.0
TX["step"] = -100
start_playing()
time.sleep(1.5)
assert not STOPPED, "a wrapping TX counter must not false-positive"
print("10. TX counter wrap does not false-positive OK")

reset()
print("bt_stall: all OK")
