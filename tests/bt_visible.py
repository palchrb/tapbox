#!/usr/bin/env python3
"""Gate the incoming pairing mode (PLAN-bt-b2-pairing.md): `bt.py
visible` opens a discoverable window with our agent as DEFAULT agent so
a car/head unit can pair the box. Verifies: the daemon endpoint plumbs
and quiesces like /bt/pair; the window sets DiscoverableTimeout BEFORE
Discoverable (dead-man switch) and resets Discoverable itself (the fake
has NO countdown — only an explicit reset passes); incoming pairs are
trusted but NEVER auto-adopted as output; the flock covers the whole
window (btwatchd collision = the documented firmware crasher); SIGKILL
mid-window leaves no stale lock and the bluez-side timeout armed; and
outside a window the box is not silently pairable.

Run ON THE RIG:   python3 tests/bt_visible.py
Local (no bus): endpoint + exit-2 sections run, dbus window SKIPs.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
BIN = os.path.join(TMP, "bin")
os.makedirs(BIN)
ARGS_LOG = os.path.join(TMP, "cli-args")
GO = "30:C0:1B:BD:13:B2"
CAR = "2C:FD:B3:5B:1C:BA"
CAR2 = "2C:FD:B3:5B:1C:BB"
CAR3 = "2C:FD:B3:5B:1C:BC"
CAR4 = "2C:FD:B3:5B:1C:BD"


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


def wait_for(what, pred, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise SystemExit(f"TIMEOUT waiting for: {what}")


# --- section A: the daemon endpoint (no dbus needed) -------------------------

os.environ["TAPBOX_STATE"] = os.path.join(TMP, "state")
os.environ["TAPBOX_TOKEN_FILE"] = os.path.join(TMP, "api-token")
os.environ["TAPBOX_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ.setdefault("TAPBOX_CACHE", os.path.join(TMP, "cache"))
os.environ["TAPBOX_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["TAPBOX_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")
os.environ["TAPBOX_BT_BACKEND"] = "cli"  # deterministic: no real bus
FAKE_CLI = os.path.join(TMP, "fake-bt-cli.sh")
write_exec(FAKE_CLI, f"""#!/bin/sh
echo "$@" >> {ARGS_LOG}
echo "==> Paired: Fake Car ({CAR})"
exit 0
""")
os.environ["TAPBOX_PLAY"] = FAKE_CLI
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from tapbox import bt as bt_mod  # noqa: E402

QUIESCED, RESUMED = [], []
daemon._bt_quiesce = lambda: QUIESCED.append(1) or True
daemon._bt_resume = lambda r: RESUMED.append(r)

srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def _box_token():
    """Privileged endpoints need the box token since the API gate landed.
    ensure() returns the daemon's existing one, or creates it when the
    daemon runs in-process here and never went through main()."""
    from tapbox import token
    return token.ensure()

def post(path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-TapBox-Token": _box_token()}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


code, body = post("/bt/visible", {"secs": 9999})
assert code == 200 and body.get("ok"), (code, body)
assert "visible 300" in open(ARGS_LOG).read(), "secs must clamp to 300"
assert QUIESCED and RESUMED, "must quiesce/resume playback like /bt/pair"
print("1. POST /bt/visible plumbs, clamps and quiesces OK")

assert bt_mod.BT_LOCK.acquire(blocking=False)
try:
    code, body = post("/bt/visible", {})
    assert code == 409, (code, body)
finally:
    bt_mod.BT_LOCK.release()
print("2. busy window -> 409 (BT_LOCK contract kept) OK")


# --- section B: exit 2 when the dbus stack is unreachable --------------------

write_exec(os.path.join(BIN, "bluetoothctl"), """#!/bin/sh
case "$1" in show) echo "Powered: yes";; esac
exit 0
""")
env2 = dict(os.environ, PATH=BIN + ":" + os.environ["PATH"],
            TAPBOX_DBUS_ADDRESS="unix:path=/nonexistent",
            DBUS_SYSTEM_BUS_ADDRESS="unix:path=/nonexistent")
r = subprocess.run([sys.executable,
                    os.path.join(REPO, "pi", "tapbox", "bt.py"),
                    "visible", "10"], env=env2, capture_output=True,
                   text=True, timeout=60)
assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
assert "pairing mode" in (r.stdout + r.stderr).lower()
print("3. no dbus stack -> exit 2 with a human message OK")


# --- section C: the real window against fake_bluezd (SKIP without dbus) ------

if not shutil.which("dbus-daemon"):
    print("SKIP window matrix: dbus-daemon not available (run on the rig)")
    sys.exit(0)
probe = subprocess.run([sys.executable, "-c", "import dbus, gi"],
                       capture_output=True)
if probe.returncode != 0:
    print("SKIP window matrix: python3-dbus/python3-gi not installed")
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


MAC_FILE = os.path.join(TMP, "bt-mac-window")
LOCK_FILE = os.path.join(TMP, "bt-window.lock")
ENV = dict(os.environ, TAPBOX_BT_BACKEND="dbus",
           DBUS_SYSTEM_BUS_ADDRESS=addr, TAPBOX_BT_FILE=MAC_FILE,
           TAPBOX_BT_LOCKFILE=LOCK_FILE,
           TAPBOX_ASOUND=os.path.join(TMP, "asound-window.conf"),
           TAPBOX_BT_CACHE_SECS="1", PATH=BIN + ":" + os.environ["PATH"])
ENV.pop("TAPBOX_DBUS_ADDRESS", None)
BT_PY = os.path.join(REPO, "pi", "tapbox", "bt.py")


def visible(*extra):
    return subprocess.Popen([sys.executable, BT_PY, "visible", *extra],
                            env=ENV, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def lock_free():
    with open(LOCK_FILE, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


try:
    mock("AddDevice", f"string:{GO}", "string:JBL GO", "boolean:true",
         "boolean:false", "int16:0")

    # 4. happy window: dead-man timeout set, default agent, explicit reset
    n0 = len(events())
    p = visible("10")
    wait_for("window discoverable", lambda: mock("GetDiscoverable",
                                                 parse="bool"))
    t = mock("GetDiscoverableTimeout", parse="uint")
    assert 0 < t <= 10, f"DiscoverableTimeout {t} not armed for the window"
    ev = events()[n0:]
    assert "Register:NoInputNoOutput" in ev and "RequestDefaultAgent" in ev, ev
    assert ev.index("Register:NoInputNoOutput") \
        < ev.index("RequestDefaultAgent"), ev
    assert p.wait(timeout=25) == 1, "empty window must exit 1"
    assert not mock("GetDiscoverable", parse="bool"), \
        "Discoverable must be reset EXPLICITLY (the fake has no timer)"
    assert "Unregister" in events()[n0:]
    assert not os.path.exists(MAC_FILE), "empty window wrote MAC_FILE"
    print("4. window arms the dead-man timeout and cleans up after OK")

    # 5. car pairs mid-window: trusted + reported, NEVER auto-adopted
    n0 = len(events())
    t0 = time.monotonic()
    p = visible("25")
    wait_for("default agent up",
             lambda: "RequestDefaultAgent" in events()[n0:])
    verdict = mock("SimulateIncomingPair", f"string:{CAR}", parse="str")
    assert verdict == "paired", verdict
    assert p.wait(timeout=15) == 0
    took = time.monotonic() - t0
    assert took < 20, f"window must close early after a pair ({took:.0f}s)"
    out = p.stdout.read()
    assert "Paired: Fake Car" in out, out
    assert mock("GetTrusted", f"string:{CAR}", parse="bool"), \
        "incoming bond must be trusted inside the window"
    assert not os.path.exists(MAC_FILE), \
        "report-only policy: MAC_FILE must be untouched"
    print("5. incoming pair -> trusted + reported, not adopted OK")

    # 6. confirm-flow car: our default agent answers the SSP dialog
    mock("AddDevice", f"string:{CAR2}", "string:Confirm Car",
         "boolean:false", "boolean:false", "int16:0")
    mock("SetPairFlow", f"string:{CAR2}", "string:confirm")
    n0 = len(events())
    p = visible("25")
    wait_for("default agent up",
             lambda: "RequestDefaultAgent" in events()[n0:])
    verdict = mock("SimulateIncomingPair", f"string:{CAR2}", parse="str")
    assert verdict == "paired", verdict
    assert "RequestConfirmation:answered" in events()[n0:], events()[n0:]
    assert p.wait(timeout=15) == 0
    print("6. confirm-flow incoming pair answered by our agent OK")

    # 7. explicit adopt arg runs the full connect flow (CLI-only option)
    n0 = len(events())
    p = visible("25", "adopt")
    wait_for("default agent up",
             lambda: "RequestDefaultAgent" in events()[n0:])
    mock("SetPcm", f"string:{CAR3}", "boolean:true")
    verdict = mock("SimulateIncomingPair", f"string:{CAR3}", parse="str")
    assert verdict == "paired", verdict
    assert p.wait(timeout=60) == 0, p.stdout.read()
    assert open(MAC_FILE).read().strip() == CAR3, "adopt must set the output"
    assert CAR3 in open(ENV["TAPBOX_ASOUND"]).read(), "adopt must route ALSA"
    os.remove(MAC_FILE)
    print("7. visible ... adopt runs the battle-tested connect path OK")

    # 8. the flock covers the whole window; a second visible fails fast
    p = visible("10")
    wait_for("window discoverable", lambda: mock("GetDiscoverable",
                                                 parse="bool"))
    assert not lock_free(), "flock must be held for the whole window"
    t0 = time.monotonic()
    r2 = subprocess.run([sys.executable, BT_PY, "visible", "10"], env=ENV,
                        capture_output=True, text=True, timeout=30)
    assert r2.returncode == 1 and time.monotonic() - t0 < 5, \
        "second window must fail fast, not queue"
    assert "another bluetooth operation" in (r2.stdout + r2.stderr)
    p.wait(timeout=25)
    assert lock_free(), "flock must release with the window"
    print("8. flock held for the window; second visible fails fast OK")

    # 9. SIGKILL mid-window: lock auto-releases, agent drops with the
    # connection, the bluez-side timeout stays armed as the only backstop
    n0 = len(events())
    p = visible("30")
    wait_for("window discoverable", lambda: mock("GetDiscoverable",
                                                 parse="bool"))
    p.kill()
    p.wait(timeout=10)
    wait_for("flock auto-release", lock_free, timeout=5)
    wait_for("agent dropped with its connection",
             lambda: "OwnerGone" in events()[n0:])
    assert mock("GetDiscoverableTimeout", parse="uint") > 0, \
        "bluez's own countdown is the only remaining protection"
    snap = subprocess.run([sys.executable, "-c", f"""
import json, sys
sys.path.insert(0, {json.dumps(os.path.join(REPO, "pi"))})
from tapbox import btbus
ok, detail = btbus.connect_device({json.dumps(GO)})
print(json.dumps(ok))
"""], env=ENV, capture_output=True, text=True, timeout=60)
    assert snap.returncode == 0 and "true" in snap.stdout.lower(), \
        (snap.stdout, snap.stderr)
    print("9. SIGKILL mid-window leaves no stale state behind OK")

    # 10. outside a window the box is not silently pairable
    verdict = mock("SimulateIncomingPair", f"string:{CAR4}", parse="str")
    assert verdict == "no-default-agent", verdict
    print("10. no window -> no default agent -> not pairable OK")

finally:
    fake.terminate()

print("bt_visible: all OK")
