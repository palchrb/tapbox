#!/usr/bin/env python3
"""B2 gate (PLAN-bt-b2-pairing.md §4): pairing over D-Bus with our own
Agent1 must classify identically to the bluetoothctl path, must never
deadlock on agent callbacks (the legacy-PIN trap, parent plan §9.1),
must stay OFF by default (TAPBOX_BT_PAIR kill switch), and must never
leak the agent registration.

Run ON THE RIG:   python3 tests/bt_pair_dbus.py
Local (no bus): the pure classification table + the cli fixture +
default-is-cli, then SKIPs the dbus matrix.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GO = "30:C0:1B:BD:13:B2"
GHOST = "AA:AA:AA:AA:AA:AA"

sys.path.insert(0, os.path.join(REPO, "pi"))
from tapbox import btbus  # noqa: E402

# one expectation table drives BOTH the unit rows and the bus matrix —
# the fixtures cannot drift apart silently
EXPECTED = {
    "org.bluez.Error.AlreadyExists": btbus.PAIR_ALREADY,
    "org.bluez.Error.AuthenticationFailed": btbus.PAIR_AUTH_FAILED,
    "org.bluez.Error.AuthenticationCanceled": btbus.PAIR_AUTH_FAILED,
    "org.bluez.Error.AuthenticationRejected": btbus.PAIR_AUTH_FAILED,
    "org.bluez.Error.AuthenticationTimeout": btbus.PAIR_AUTH_FAILED,
    "org.bluez.Error.ConnectionAttemptFailed": btbus.PAIR_NOT_AVAILABLE,
    "org.freedesktop.DBus.Error.UnknownObject": btbus.PAIR_NOT_AVAILABLE,
    "org.freedesktop.DBus.Error.UnknownMethod": btbus.PAIR_NOT_AVAILABLE,
    "org.bluez.Error.InProgress": btbus.PAIR_ERROR,
    "org.bluez.Error.Failed": btbus.PAIR_ERROR,
    "org.freedesktop.DBus.Error.NoReply": btbus.PAIR_ERROR,
}

for name, want in EXPECTED.items():
    got, detail = btbus._map_pair_error(name, "x")
    assert got == want, f"{name}: {got} != {want}"
    assert name in detail  # the typed name must survive into the log
print("1. _map_pair_error classification table OK")


# --- cli fixture: the regex path is the escape hatch — keep it healthy ------

def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


TMP = tempfile.mkdtemp()
BIN = os.path.join(TMP, "bin")
os.makedirs(BIN)
MODE = os.path.join(TMP, "pair-mode")
CTL_LOG = os.path.join(TMP, "ctl-log")
write_exec(os.path.join(BIN, "bluetoothctl"), f"""#!/bin/sh
echo "$@" >> {CTL_LOG}
case "$1" in
  pair)
    case "$(cat {MODE})" in
      ok)       echo "Pairing successful"; exit 0;;
      already)  echo "Failed to pair: org.bluez.Error.AlreadyExists"; exit 1;;
      auth)     echo "Failed to pair: org.bluez.Error.AuthenticationFailed"; exit 1;;
      notavail) echo "Device {GO} not available"; exit 1;;
      *)        echo "Failed to pair: org.bluez.Error.Failed"; exit 1;;
    esac;;
esac
exit 0
""")


def run_snippet(env, code, timeout=120):
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SystemExit(f"snippet failed:\n{r.stdout}\n{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1]), r.stdout


PAIR_SNIPPET = f"""
import json, sys
sys.path.insert(0, {json.dumps(os.path.join(REPO, "pi"))})
from tapbox import btbus
v, detail = btbus.pair({json.dumps(GO)})
print(json.dumps({{"verdict": v, "detail": detail,
                   "backend": btbus.backend()}}))
"""

CLI_EXPECT = {"ok": btbus.PAIR_OK, "already": btbus.PAIR_ALREADY,
              "auth": btbus.PAIR_AUTH_FAILED,
              "notavail": btbus.PAIR_NOT_AVAILABLE,
              "failed": btbus.PAIR_ERROR}
env_cli = dict(os.environ, TAPBOX_BT_BACKEND="cli",
               PATH=BIN + ":" + os.environ["PATH"])
env_cli.pop("TAPBOX_BT_PAIR", None)
for mode, want in CLI_EXPECT.items():
    with open(MODE, "w") as f:
        f.write(mode)
    snap, _out = run_snippet(env_cli, PAIR_SNIPPET)
    assert snap["verdict"] == want, (mode, snap)
print("2. cli pair classification unchanged (escape hatch healthy) OK")

# default is cli: with the kill switch unset the bluetoothctl fork runs
open(CTL_LOG, "w").close()
with open(MODE, "w") as f:
    f.write("ok")
snap, _out = run_snippet(env_cli, PAIR_SNIPPET)
assert "pair" in open(CTL_LOG).read(), "cli fork did not run"
print("3. TAPBOX_BT_PAIR unset -> bluetoothctl path used OK")


# --- the dbus matrix (private bus + fake_bluezd) -----------------------------

if not shutil.which("dbus-daemon"):
    print("SKIP dbus side: dbus-daemon not available (run on the rig)")
    sys.exit(0)
probe = subprocess.run([sys.executable, "-c", "import dbus, gi"],
                       capture_output=True)
if probe.returncode != 0:
    print("SKIP dbus side: python3-dbus/python3-gi not installed")
    sys.exit(0)

addr = subprocess.run(["dbus-daemon", "--session", "--print-address",
                       "--fork"], capture_output=True, text=True,
                      check=True).stdout.strip()
fake = subprocess.Popen(
    [sys.executable, os.path.join(REPO, "tests", "fake_bluezd.py")],
    env=dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=addr),
    stdout=subprocess.PIPE, text=True)
assert "ready" in fake.stdout.readline()
CALL = ["dbus-send", "--bus=" + addr, "--print-reply",
        "--dest=org.bluez", "--type=method_call", "/org/tapbox/mock"]


def mock(method, *args, parse=None):
    r = subprocess.run(CALL + [f"org.tapbox.Mock.{method}", *args],
                       check=True, capture_output=True, text=True)
    if parse == "int":
        return int(re.search(r"int32 (-?\d+)", r.stdout).group(1))
    if parse == "uint":
        return int(re.search(r"uint32 (\d+)", r.stdout).group(1))
    if parse == "bool":
        return "true" in r.stdout
    if parse == "str":
        return re.search(r'string "([^"]*)"', r.stdout).group(1)
    if parse == "list":
        return re.findall(r'string "([^"]*)"', r.stdout)
    return r.stdout


def events():
    return mock("GetAgentEvents", parse="list")


def env_dbus(**extra):
    env = dict(os.environ, TAPBOX_BT_BACKEND="dbus",
               DBUS_SYSTEM_BUS_ADDRESS=addr, TAPBOX_BT_PAIR="dbus",
               TAPBOX_BT_FILE=os.path.join(TMP, "bt-mac"),
               TAPBOX_BT_LOCKFILE=os.path.join(TMP, "bt.lock"),
               TAPBOX_ASOUND=os.path.join(TMP, "asound.conf"),
               TAPBOX_BT_CACHE_SECS="1", TAPBOX_SCAN_SECS="1",
               PATH=BIN + ":" + os.environ["PATH"])
    env.update(extra)
    return env


try:
    mock("AddDevice", f"string:{GO}", "string:JBL GO", "boolean:false",
         "boolean:false", "int16:0")

    # 4. just-works OK + agent registered before, unregistered after
    n0 = len(events())
    t0 = time.monotonic()
    snap, _out = run_snippet(env_dbus(), PAIR_SNIPPET)
    assert snap["backend"] == "dbus" and snap["verdict"] == btbus.PAIR_OK, snap
    assert time.monotonic() - t0 < 30, "pair took suspiciously long"
    ev = events()[n0:]
    assert ev and ev[0] == "Register:NoInputNoOutput" and "Unregister" in ev, ev
    assert mock("GetPairCount", f"string:{GO}", parse="int") == 1
    print("4. dbus just-works pair OK (agent register/unregister clean)")

    # 5. confirm flow: RequestConfirmation dispatched and answered
    mock("SetPairFlow", f"string:{GO}", "string:confirm")
    snap, _out = run_snippet(env_dbus(), PAIR_SNIPPET)
    assert snap["verdict"] == btbus.PAIR_OK, snap
    assert "RequestConfirmation:answered" in events(), events()[-5:]
    print("5. confirm flow answered by our agent OK")

    # 6. THE deadlock regression: Pair completes only after the agent
    # answers RequestPinCode — a blocking Pair() hangs here and fails
    # via the fake's 15s timeout instead of returning ok
    mock("SetPairFlow", f"string:{GO}", "string:pin")
    snap, _out = run_snippet(env_dbus(), PAIR_SNIPPET)
    assert snap["verdict"] == btbus.PAIR_OK, ("DEADLOCK? agent callbacks "
                                              "not dispatched", snap)
    assert "RequestPinCode:answered:0000" in events(), events()[-5:]
    mock("SetPairFlow", f"string:{GO}", "string:just-works")
    print("6. legacy-PIN flow completes — no agent-dispatch deadlock OK")

    # 7. full error matrix over the bus
    BUS_MATRIX = {"already": btbus.PAIR_ALREADY,
                  "auth-failed": btbus.PAIR_AUTH_FAILED,
                  "auth-timeout": btbus.PAIR_AUTH_FAILED,
                  "not-available": btbus.PAIR_NOT_AVAILABLE,
                  "in-progress": btbus.PAIR_ERROR,
                  "failed": btbus.PAIR_ERROR}
    for verdict, want in BUS_MATRIX.items():
        mock("SetPairResult", f"string:{GO}", f"string:{verdict}")
        snap, _out = run_snippet(env_dbus(), PAIR_SNIPPET)
        assert snap["verdict"] == want, (verdict, snap)
    print("7. dbus error matrix classifies like the cli path OK")

    # 8. agent is not leaked on failure paths (finally-unregister)
    ev = events()
    assert ev.count("Register:NoInputNoOutput") == ev.count("Unregister"), ev
    print("8. every Register has its Unregister (incl. failures) OK")

    # 9. AlreadyExists still trusts + connects (full bt.py use flow).
    # Reset to unpaired first — scenario 4's ok-pair flipped Paired on,
    # which would skip the pair branch entirely.
    mock("DropDevice", f"string:{GO}")
    mock("AddDevice", f"string:{GO}", "string:JBL GO", "boolean:false",
         "boolean:false", "int16:0")
    mock("SetPairResult", f"string:{GO}", "string:already")
    mock("SetPcm", f"string:{GO}", "boolean:true")
    r = subprocess.run([sys.executable, os.path.join(REPO, "pi", "tapbox",
                                                     "bt.py"), "use", GO],
                       env=env_dbus(), capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert mock("GetTrusted", f"string:{GO}", parse="bool"), \
        "AlreadyExists path lost the trust fix"
    print("9. AlreadyExists -> trusted + connected via bt.py use OK")

    # 10. stale-key clear-and-retry fires EXACTLY once. Fresh unpaired
    # device state (DropDevice resets its counters; REMOVES persists),
    # in pairing mode so the removed device re-appears on the retry's
    # discovery, and a result queue that heals on the second attempt.
    mock("DropDevice", f"string:{GO}")
    mock("AddDevice", f"string:{GO}", "string:JBL GO", "boolean:false",
         "boolean:false", "int16:0")
    mock("SetPairingMode", f"string:{GO}", "boolean:true")
    mock("SetPairResult", f"string:{GO}", "string:auth-failed ok")
    removes0 = mock("GetRemoveCount", f"string:{GO}", parse="int")
    r = subprocess.run([sys.executable, os.path.join(REPO, "pi", "tapbox",
                                                     "bt.py"), "use", GO],
                       env=env_dbus(), capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert mock("GetRemoveCount", f"string:{GO}",
                parse="int") == removes0 + 1, "clear-bond must run ONCE"
    assert mock("GetPairCount", f"string:{GO}",
                parse="int") == 2, "expected exactly two pairs"
    print("10. stale-key clear-and-retry runs exactly once OK")

    # 11. never-seen device: no bond cleared, guidance printed
    r = subprocess.run([sys.executable, os.path.join(REPO, "pi", "tapbox",
                                                     "bt.py"), "use", GHOST],
                       env=env_dbus(), capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "not seen during scan" in (r.stdout + r.stderr).lower() \
        or "pairing mode" in (r.stdout + r.stderr).lower()
    assert mock("GetRemoveCount", f"string:{GHOST}", parse="int") == 0, \
        "must NOT clear bonds for absent devices"
    print("11. never-seen device -> not-available, bond untouched OK")

    # 12. kill switch honored under the dbus backend: unset -> cli fork
    open(CTL_LOG, "w").close()
    with open(MODE, "w") as f:
        f.write("ok")
    pairs_before = mock("GetPairCount", f"string:{GO}", parse="int")
    env_off = env_dbus()
    env_off.pop("TAPBOX_BT_PAIR")
    snap, _out = run_snippet(env_off, PAIR_SNIPPET)
    assert snap["verdict"] == btbus.PAIR_OK, snap
    assert "pair" in open(CTL_LOG).read(), "expected the bluetoothctl fork"
    assert mock("GetPairCount", f"string:{GO}",
                parse="int") == pairs_before, "dbus Pair ran without opt-in"
    print("12. kill switch: default stays on bluetoothctl OK")

    # 13. bus-down degrade: dbus pair falls back to cli, loudly
    open(CTL_LOG, "w").close()
    env_dead = env_dbus(DBUS_SYSTEM_BUS_ADDRESS="unix:path=/nonexistent",
                        TAPBOX_DBUS_ADDRESS="unix:path=/nonexistent")
    snap, out = run_snippet(env_dead, PAIR_SNIPPET)
    assert snap["verdict"] == btbus.PAIR_OK, snap
    assert "cli fallback" in out, out
    assert "pair" in open(CTL_LOG).read()
    print("13. dead bus degrades to the cli path (logged) OK")

finally:
    fake.terminate()

print("bt_pair_dbus: all OK")
