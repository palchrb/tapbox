#!/usr/bin/env python3
"""Gate the recovery hardening (review 2026-07-18 R6). Three promises:
(1) recover()'s stubborn-wedge branch rfkill-blocks the radio around the
second firmware re-attach — anything raised in between must still run
the unblock, or the radio stays down until reboot with no healer able
to reach it; (2) /bt/connect gives the helper 240s, not 90 — a connect
that runs a full recover() (two re-attach rounds + the rfkill
power-cycle) must never be SIGKILLed between block and unblock; (3)
install.sh ships a bluetooth.service Restart=on-failure drop-in, the
only healer for a bluetoothd death WITHOUT the firmware-crash kernel
signature (btwatchd is passive on adapter loss by design)."""
import json
import os
import re
import sys
import tempfile
import threading
import time as _time
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = os.path.join(TMP, "state")
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "api-token")
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ.setdefault("VIBB_CACHE", os.path.join(TMP, "cache"))
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import bt as bt_mod  # noqa: E402


class TimeShim:
    """No-op sleep for the bt module ONLY — mutating the global time
    module while other threads live is the Q2 flake mechanism."""
    sleep = staticmethod(lambda s: None)

    def __getattr__(self, name):
        return getattr(_time, name)


RUNS = []


def fake_run(cmd, timeout=30):
    RUNS.append(tuple(cmd))
    return 0, ""


REATTACH = [0]


def reattach_boom():
    REATTACH[0] += 1
    if REATTACH[0] >= 2:  # the in-block second attempt blows up
        raise RuntimeError("serdev rebind exploded")
    return True


bt_mod._run = fake_run
bt_mod.time = TimeShim()
bt_mod.btbus.adapter_power_on = lambda: None
bt_mod.controller_ok = lambda: False  # stubborn wedge: both rounds run


def unblock_after_block():
    """Was there an rfkill unblock AFTER the last rfkill block?"""
    blocks = [i for i, c in enumerate(RUNS)
              if c[:2] == ("rfkill", "block")]
    unblocks = [i for i, c in enumerate(RUNS)
                if c[:2] == ("rfkill", "unblock")]
    return bool(blocks) and bool(unblocks) and max(unblocks) > max(blocks)


# 1. the second re-attach raises between block and unblock -> the
# unblock still runs (try/finally), the error still surfaces
bt_mod._reattach_firmware = reattach_boom
raised = False
try:
    bt_mod.recover()
except RuntimeError:
    raised = True
assert raised, "the failure must still surface to the caller"
assert unblock_after_block(), \
    f"radio left rfkill-blocked after a failed re-attach: {RUNS}"
print("1. exception mid-power-cycle still unblocks the radio OK")

# 2. the plain stubborn-wedge path keeps its block -> unblock ordering
RUNS.clear()
REATTACH[0] = -10  # never raises now
bt_mod.recover()
assert unblock_after_block(), f"unblock must follow block: {RUNS}"
print("2. stubborn-wedge power-cycle ends unblocked OK")

bt_mod.time = _time

# 3. /bt/connect passes the helper 240s (a full recover() fits);
# forget/disconnect keep their 30s
import daemon  # noqa: E402

CAPTURED = []
daemon.bt_action = (lambda args, timeout: CAPTURED.append((args, timeout))
                    or {"ok": True})
daemon._bt_quiesce = lambda: False
daemon._bt_resume = lambda r: None

srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def _box_token():
    """Privileged endpoints need the box token since the API gate landed.
    ensure() returns the daemon's existing one, or creates it when the
    daemon runs in-process here and never went through main()."""
    from vibb import token
    return token.ensure()

def post(path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-Vibb-Token": _box_token()}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


MAC = "AA:BB:CC:DD:EE:FF"
code, _ = post("/bt/connect", {"mac": MAC})
assert code == 200 and CAPTURED == [(["use", MAC], 240)], (code, CAPTURED)
CAPTURED.clear()
code, _ = post("/bt/disconnect", {"mac": MAC})
assert code == 200 and CAPTURED == [(["disconnect", MAC], 30)], CAPTURED
srv.shutdown()
print("3. /bt/connect budgets 240s (recover fits), disconnect stays 30s OK")

# 4. install.sh ships the bluetoothd crash healer: a bluetooth.service
# drop-in with Restart=on-failure
with open(os.path.join(REPO, "pi", "install.sh")) as f:
    sh = f.read()
blocks = re.findall(r"bluetooth\.service\.d/[\w.-]+\.conf\s*<<'EOF'\n"
                    r"(.*?)\nEOF", sh, re.S)
assert any("Restart=on-failure" in b for b in blocks), \
    "install.sh must drop Restart=on-failure into bluetooth.service"
print("4. install.sh installs the bluetoothd on-failure restart drop-in OK")

# 5. crash-signature detection (field 2026-07-27 21:47): a chip whose
# COMMAND path times out counts as crashed even with the UP flag set;
# link tx timeouts alone (peer out of range) never do; the hard
# hardware-error signature still requires the controller to be down.
DEGRADED = ("Bluetooth: hci0: link tx timeout\n"
            "Bluetooth: hci0: killing stalled connection b4:ec:02:4f:36:7c\n"
            "Bluetooth: hci0: command 0x0406 tx timeout\n"
            "Bluetooth: hci0: command 0x0406 tx timeout\n"
            "Bluetooth: hci0: command 0x0c03 tx timeout\n")
RANGE_LOSS = ("Bluetooth: hci0: link tx timeout\n" * 3
              + "Bluetooth: hci0: killing stalled connection aa:bb\n")
HARD = "Bluetooth: hci0: hardware error 0x00\n"
real_run, real_up = bt_mod._run, bt_mod._hci_up
try:
    bt_mod._hci_up = lambda: True
    bt_mod._run = lambda cmd, timeout=30: (0, DEGRADED)
    assert bt_mod._hci_crashed(), \
        "3+ command tx timeouts = crashed, UP flag notwithstanding"
    bt_mod._run = lambda cmd, timeout=30: (0, RANGE_LOSS)
    assert not bt_mod._hci_crashed(), \
        "link tx timeouts alone are a peer out of range, not a crash"
    bt_mod._run = lambda cmd, timeout=30: (0, HARD)
    assert not bt_mod._hci_crashed(), \
        "hardware-error signature with the controller UP is stale log"
    bt_mod._hci_up = lambda: False
    assert bt_mod._hci_crashed(), "down + hardware error = crashed"
    bt_mod._run = lambda cmd, timeout=30: (0, "")
    assert not bt_mod._hci_crashed(), \
        "down with no signature = rfkill/powered-down, not a crash"
finally:
    bt_mod._run, bt_mod._hci_up = real_run, real_up
print("5. crash signatures: command-timeout wedge detected, range loss "
      "and stale logs ignored OK")

print("BT RECOVER GUARD OK — the radio can't be stranded blocked, the "
      "connect endpoint outlives a full recovery, and a crashed "
      "bluetoothd restarts itself.")
