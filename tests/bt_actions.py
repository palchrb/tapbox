#!/usr/bin/env python3
"""Phase B1 gate (PLAN-bt-dbus.md §3/§8): the agent-less ACTIONS —
connect/disconnect/trust/remove — must classify identically under the
cli and dbus backends. Pairing is deliberately absent (phase B2).

Run ON THE RIG:   python3 tests/bt_actions.py

Local (no bus): runs the pure error-mapping unit tests + the cli
classification fixture, then SKIPs the dbus matrix.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GO = "30:C0:1B:BD:13:B2"


def unit_map_tests():
    sys.path.insert(0, os.path.join(REPO, "pi"))
    from tapbox import btbus
    ok, d = btbus._map_connect_error("org.bluez.Error.AlreadyConnected", "x")
    assert ok is True, d
    ok, d = btbus._map_connect_error("org.bluez.Error.Failed",
                                     "br-connection-page-timeout")
    assert ok is False and "page-timeout" in d
    ok, d = btbus._map_disconnect_error("org.bluez.Error.NotConnected", "x")
    assert ok is True
    v, _ = btbus._map_remove_error("org.bluez.Error.DoesNotExist", "x")
    assert v == btbus.REMOVE_NOT_FOUND
    v, _ = btbus._map_remove_error("org.freedesktop.DBus.Error.UnknownObject",
                                   "x")
    assert v == btbus.REMOVE_NOT_FOUND
    v, _ = btbus._map_remove_error("org.bluez.Error.Failed", "x")
    assert v == btbus.REMOVE_ERROR
    print("error-mapping unit tests OK")


def run_snippet(env, code):
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"snippet failed:\n{r.stdout}\n{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


SNIPPET = f"""
import json, sys
sys.path.insert(0, {json.dumps(os.path.join(REPO, "pi"))})
from tapbox import btbus
out = {{
  "backend": btbus.backend(),
  "connect_ok": btbus.connect_device({json.dumps(GO)}),
  "disconnect_ok": btbus.disconnect_device({json.dumps(GO)}),
  "disconnect_again": btbus.disconnect_device({json.dumps(GO)}),
  "remove_missing": btbus.remove_device("AA:AA:AA:AA:AA:AA"),
}}
print(json.dumps(out))
"""


def normalize(snap):
    """Classifications only — detail strings legitimately differ."""
    return {
        "connect_ok": snap["connect_ok"][0],
        "disconnect_ok": snap["disconnect_ok"][0],
        "disconnect_again": snap["disconnect_again"][0],
        "remove_missing": snap["remove_missing"][0],
    }


EXPECTED = {
    "connect_ok": True,
    "disconnect_ok": True,
    "disconnect_again": True,   # NotConnected counts as success
    "remove_missing": "not-found",
}


def cli_bin(tmp):
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    state = os.path.join(tmp, "connected")
    ctl = f"""#!/bin/sh
case "$1" in
  connect) touch {state}; exit 0;;
  disconnect)
    if [ -e {state} ]; then rm {state}; echo "Successful disconnected"; exit 0
    else echo "Failed to disconnect: org.bluez.Error.NotConnected"; exit 0; fi;;
  remove) echo "Device $2 not available"; exit 1;;
esac
exit 0
"""
    open(os.path.join(bindir, "bluetoothctl"), "w").write(ctl)
    os.chmod(os.path.join(bindir, "bluetoothctl"), 0o755)
    return bindir


def env_for(tmp, backend):
    env = dict(os.environ, TAPBOX_BT_BACKEND=backend,
               TAPBOX_BT_FILE=os.path.join(tmp, "bt-mac"),
               TAPBOX_BT_LOCKFILE=os.path.join(tmp, "bt.lock"),
               TAPBOX_ASOUND=os.path.join(tmp, "asound.conf"))
    return env


def main():
    unit_map_tests()
    tmp = tempfile.mkdtemp()

    env_cli = env_for(tmp, "cli")
    env_cli["PATH"] = cli_bin(tmp) + ":" + env_cli["PATH"]
    cli = normalize(run_snippet(env_cli, SNIPPET))
    assert cli == EXPECTED, cli
    print("cli action classifications OK")

    if not shutil.which("dbus-daemon"):
        print("SKIP dbus side: dbus-daemon not available (run on the rig)")
        return 0
    probe = subprocess.run([sys.executable, "-c", "import dbus, gi"],
                           capture_output=True)
    if probe.returncode != 0:
        print("SKIP dbus side: python3-dbus/python3-gi not installed")
        return 0

    addr = subprocess.run(["dbus-daemon", "--session", "--print-address",
                           "--fork"], capture_output=True, text=True,
                          check=True).stdout.strip()
    fake_env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=addr)
    fake = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "tests", "fake_bluezd.py")],
        env=fake_env, stdout=subprocess.PIPE, text=True)
    assert "ready" in fake.stdout.readline()
    call = ["dbus-send", "--bus=" + addr, "--print-reply",
            "--dest=org.bluez", "--type=method_call", "/org/tapbox/mock"]
    subprocess.run(call + ["org.tapbox.Mock.AddDevice", f"string:{GO}",
                           "string:JBL GO", "boolean:true", "boolean:false",
                           "int16:0"], check=True, capture_output=True)
    try:
        env_dbus = env_for(tmp, "dbus")
        env_dbus["DBUS_SYSTEM_BUS_ADDRESS"] = addr
        snap = run_snippet(env_dbus, SNIPPET)
        assert snap["backend"] == "dbus", snap
        db = normalize(snap)
        if db != EXPECTED:
            print("ACTION MISMATCH:")
            print("dbus:", json.dumps(db, indent=2))
            print("want:", json.dumps(EXPECTED, indent=2))
            return 1

        # the error repertoire, dbus side only (cli can't fake these)
        subprocess.run(call + ["org.tapbox.Mock.SetConnectResult",
                               f"string:{GO}", "string:already-connected"],
                       check=True, capture_output=True)
        snap2 = run_snippet(env_dbus, f"""
import json, sys
sys.path.insert(0, {json.dumps(os.path.join(REPO, "pi"))})
from tapbox import btbus
ok, detail = btbus.connect_device({json.dumps(GO)})
t = btbus.trust({json.dumps(GO)})
print(json.dumps({{"already": ok, "detail": detail}}))
""")
        assert snap2["already"] is True, snap2
        r = subprocess.run(call + ["org.tapbox.Mock.GetTrusted",
                                   f"string:{GO}"], check=True,
                           capture_output=True, text=True)
        assert "true" in r.stdout, r.stdout
        print("dbus error repertoire OK (AlreadyConnected=success, "
              "Trusted set)")
    finally:
        fake.terminate()

    print("B1 ACTIONS PARITY OK — flip actions live is safe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
