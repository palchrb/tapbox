#!/usr/bin/env python3
"""Phase A parity gate (PLAN-bt-dbus.md §8): the dbus backend must
return the SAME shapes as the cli backend for the read primitives.

Run ON THE RIG (needs dbus-daemon, python3-dbus, python3-gi):

    python3 tests/bt_parity.py

It starts a private bus + fake_bluezd.py, seeds two devices, and
compares bt_status()/device_info()/a2dp_pcm_present() under
TAPBOX_BT_BACKEND=dbus against the expected fixture (which the cli
backend produces from equivalent bluetoothctl output — asserted here
via PATH fakes, no real radio touched).

Exit 0 = parity holds; nonzero prints the diff. When this passes on the
rig, flip `auto` to prefer dbus in btbus.backend() (phase A1 done).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GO = "30:C0:1B:BD:13:B2"
JR = "2C:FD:B3:5B:1C:BA"

EXPECTED_STATUS_DEVICES = [  # bt_status sorts by name
    {"mac": GO, "name": "JBL GO", "audio": True,
     "paired": True, "connected": False},
    {"mac": JR, "name": "JBL JR310BT", "audio": True,
     "paired": True, "connected": True},
]


def fresh_env(tmp, backend):
    env = dict(os.environ,
               TAPBOX_BT_BACKEND=backend,
               TAPBOX_BT_FILE=os.path.join(tmp, "bt-mac"),
               TAPBOX_BT_LOCKFILE=os.path.join(tmp, "bt.lock"),
               TAPBOX_ASOUND=os.path.join(tmp, "asound.conf"),
               TAPBOX_STATE=os.path.join(tmp, "state"),
               TAPBOX_SETTINGS=os.path.join(tmp, "se.json"),
               TAPBOX_LIBRARY=os.path.join(tmp, "l.json"),
               TAPBOX_CACHE=os.path.join(tmp, "cache"))
    os.makedirs(env["TAPBOX_STATE"], exist_ok=True)
    return env


def snapshot(env):
    """Run the read primitives in a fresh interpreter, return JSON."""
    code = f"""
import json, sys
sys.path.insert(0, {json.dumps(os.path.join(REPO, "pi"))})
from tapbox import bt, btbus
out = {{
  "backend": btbus.backend(),
  "status": bt.bt_status(),
  "info_go": btbus.device_info({json.dumps(GO)}),
  "info_missing": btbus.device_info("AA:AA:AA:AA:AA:AA"),
  "pcm_jr": btbus.a2dp_pcm_present({json.dumps(JR)}),
  "pcm_go": btbus.a2dp_pcm_present({json.dumps(GO)}),
  "powered": btbus.adapter_powered(),
}}
print(json.dumps(out))
"""
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise SystemExit(f"snapshot failed:\n{r.stdout}\n{r.stderr}")
    lines = r.stdout.strip().splitlines()
    noise = [ln for ln in lines[:-1]] + r.stderr.strip().splitlines()
    return json.loads(lines[-1]), noise


def cli_fixture_bin(tmp):
    """PATH fakes producing bluetoothctl/bluealsa-aplay output equivalent
    to the seeded dbus fixture."""
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir, exist_ok=True)
    ctl = f"""#!/bin/sh
case "$1 $2" in
  "devices Paired") printf 'Device {GO} JBL GO\\nDevice {JR} JBL JR310BT\\n';;
  "devices Connected") printf 'Device {JR} JBL JR310BT\\n';;
  "info {GO}") printf 'Device {GO} (public)\\n\\tAlias: JBL GO\\n\\tPaired: yes\\n\\tConnected: no\\n\\tIcon: audio-card\\n\\tUUID: Audio Sink (0000110b-0000-1000-8000-00805f9b34fb)\\n';;
  "info {JR}") printf 'Device {JR} (public)\\n\\tAlias: JBL JR310BT\\n\\tPaired: yes\\n\\tConnected: yes\\n\\tIcon: audio-headset\\n\\tUUID: Audio Sink (0000110b-0000-1000-8000-00805f9b34fb)\\n';;
  "info AA:AA:AA:AA:AA:AA") echo "Device AA:AA:AA:AA:AA:AA not available";;
  "show ") echo "Powered: yes";;
esac
exit 0
"""
    open(os.path.join(bindir, "bluetoothctl"), "w").write(ctl)
    aplay = f"""#!/bin/sh
echo 'bluealsa:DEV={JR},PROFILE=a2dp'
exit 0
"""
    open(os.path.join(bindir, "bluealsa-aplay"), "w").write(aplay)
    for f in ("bluetoothctl", "bluealsa-aplay"):
        os.chmod(os.path.join(bindir, f), 0o755)
    return bindir


def seed_fake_bluezd(bus_addr):
    env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=bus_addr)
    proc = subprocess.Popen([sys.executable,
                             os.path.join(REPO, "tests", "fake_bluezd.py")],
                            env=env, stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    assert "ready" in line, line
    call = ["dbus-send", "--bus=" + bus_addr, "--print-reply",
            "--dest=org.bluez", "--type=method_call", "/org/tapbox/mock"]
    subprocess.run(call + ["org.tapbox.Mock.AddDevice",
                           f"string:{GO}", "string:JBL GO",
                           "boolean:true", "boolean:false", "int16:0"],
                   check=True)
    subprocess.run(call + ["org.tapbox.Mock.AddDevice",
                           f"string:{JR}", "string:JBL JR310BT",
                           "boolean:true", "boolean:true", "int16:0"],
                   check=True)
    for mac in (GO, JR):  # both fixtures are speakers — say so, or the
        # parity check would compare two backends that both just fail
        # to detect audio (bt_speaker_only gates the flag itself)
        subprocess.run(call + ["org.tapbox.Mock.SetUuids", f"string:{mac}",
                               "string:0000110b-0000-1000-8000-00805f9b34fb"],
                       check=True)
    subprocess.run(call + ["org.tapbox.Mock.SetPcm",
                           f"string:{JR}", "boolean:true"], check=True)
    return proc


def normalize(snap):
    return {"status_devices": snap["status"]["devices"],
            "info_go": snap["info_go"],
            "info_missing": snap["info_missing"],
            "pcm_jr": snap["pcm_jr"], "pcm_go": snap["pcm_go"],
            "powered": snap["powered"]}


def main():
    tmp = tempfile.mkdtemp()

    # cli side: PATH fakes — runs everywhere, validates the fixture
    env_cli = fresh_env(tmp, "cli")
    env_cli["PATH"] = cli_fixture_bin(tmp) + ":" + env_cli["PATH"]
    cli_snap, _ = snapshot(env_cli)
    cli = normalize(cli_snap)
    assert cli["status_devices"] == EXPECTED_STATUS_DEVICES, cli
    assert cli["info_go"] == {"present": True, "paired": True,
                              "connected": False, "name": "JBL GO"}, cli
    assert cli["info_missing"]["present"] is False
    assert cli["pcm_jr"] is True and cli["pcm_go"] is False
    print("cli fixture OK")

    if not shutil.which("dbus-daemon"):
        print("SKIP dbus side: dbus-daemon not available (run on the rig)")
        return 0
    probe = subprocess.run(
        [sys.executable, "-c", "import dbus, gi"], capture_output=True)
    if probe.returncode != 0:
        print("SKIP dbus side: python3-dbus/python3-gi not installed —")
        print("  sudo apt install python3-dbus python3-gi")
        print("  (or just: sudo ./pi/install.sh — it installs them now)")
        return 0

    # dbus side: private bus + fake service, same logical state
    addr = subprocess.run(["dbus-daemon", "--session", "--print-address",
                           "--fork"], capture_output=True, text=True,
                          check=True).stdout.strip()
    fake = seed_fake_bluezd(addr)
    try:
        env_dbus = fresh_env(tmp, "dbus")
        env_dbus["DBUS_SYSTEM_BUS_ADDRESS"] = addr
        dbus_snap, noise = snapshot(env_dbus)
        assert dbus_snap["backend"] == "dbus", \
            f"dbus backend not selected: {dbus_snap['backend']}"
        db = normalize(dbus_snap)
    finally:
        fake.terminate()

    if cli == db:
        print("PARITY OK — cli and dbus backends agree:")
        print(json.dumps(db, indent=2))
        print("\nNext: flip 'auto' to prefer dbus in btbus.backend().")
        return 0
    print("PARITY MISMATCH:")
    print("cli :", json.dumps(cli, indent=2, sort_keys=True))
    print("dbus:", json.dumps(db, indent=2, sort_keys=True))
    if noise:
        print("dbus-side diagnostics (backend fallbacks etc.):")
        for ln in noise:
            print("  |", ln)
    return 1


if __name__ == "__main__":
    sys.exit(main())
