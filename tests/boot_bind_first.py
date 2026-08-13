#!/usr/bin/env python3
"""Gate the boot bind order (review 2026-07-18 B1): main() must bind the
HTTP server BEFORE the wifi boot re-enable runs its rfkill/iw probes
(three subprocess spawns with up-to-5s timeouts) — the screen's very
first /system poll used to queue behind them and sat on the splash.
The re-enable itself must still happen: a box whose wifi was left 'off'
in the PWA has to come back reachable after a power cycle."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = os.path.join(TMP, "state")
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ.setdefault("VIBB_CACHE", os.path.join(TMP, "cache"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_BIND"] = "127.0.0.1"
# pick a free port for the API; portal on an ephemeral one (0 is fine —
# it must only not collide with a privileged-bind failure loop)
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
PORT = _s.getsockname()[1]
_s.close()
os.environ["VIBB_PORT"] = str(PORT)
os.environ["VIBB_PORTAL_PORT"] = "0"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

BLOCKED = threading.Event()  # the slow rfkill/iw probe "runs" until set
STATE_CALLS = []
SET_CALLS = []


def slow_wifi_state():
    STATE_CALLS.append(time.monotonic())
    BLOCKED.wait(30)  # the boot-time probes, frozen mid-flight
    return False, None, None  # wifi was left off — must be re-enabled


daemon.wifi_state = slow_wifi_state
daemon.set_wifi = lambda enabled: SET_CALLS.append(enabled) or {}

threading.Thread(target=daemon.main, daemon=True).start()


def get(path, timeout=5):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
        return r.status, json.loads(r.read())


# 1. the server answers while the wifi re-enable is still stuck in its
# probes — the bind never waits on them
deadline = time.monotonic() + 10
code = None
while time.monotonic() < deadline:
    try:
        code, body = get("/settings", timeout=2)
        break
    except OSError:
        time.sleep(0.05)
assert code == 200, "server never came up while wifi probes were stuck"
assert not SET_CALLS, "re-enable finished before the probe — not blocked?"
print("1. HTTP server binds while the wifi re-enable is still probing OK")

# 2. the re-enable still runs to completion off-thread: wifi read as
# 'off' gets switched back on (headless box stays reachable)
BLOCKED.set()
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not SET_CALLS:
    time.sleep(0.02)
assert STATE_CALLS, "wifi state was never probed"
assert SET_CALLS == [True], f"wifi left off must be re-enabled: {SET_CALLS}"
print("2. wifi left off is still re-enabled after the bind OK")

print("BOOT BIND FIRST OK — the screen gets its API the moment the "
      "daemon starts; the wifi rescue happens in the background.")
