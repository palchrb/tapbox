#!/usr/bin/env python3
"""Phase C gate (PLAN-bt-dbus.md §7): the event-driven reconnect daemon
(btwatchd) against fake-bluezd on a private bus. Verifies the event
paths the bash poll loop could only cover with a 60s worst case:

  1. connect on start (BOOT window)
  2. reconnect within seconds when the target drops
  3. instant retarget when the MAC file changes
  4. re-entry through the BOOT fast window after a bluez restart
  5. flock deference: no Connect while another process owns the radio

Run ON THE RIG (or any machine with python3-dbus/python3-gi):
    python3 tests/bt_reconnect.py
SKIPs cleanly where dbus-daemon or the python bindings are missing.
"""

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GO = "30:C0:1B:BD:13:B2"
JR = "2C:FD:B3:5B:1C:BA"


def wait_for(what, pred, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.2)
    raise SystemExit(f"TIMEOUT waiting for: {what}")


def main():
    if not shutil.which("dbus-daemon"):
        print("SKIP: dbus-daemon not available (run on the rig)")
        return 0
    try:
        import dbus
    except ImportError:
        print("SKIP: python3-dbus not installed")
        return 0
    probe = subprocess.run([sys.executable, "-c", "import gi"],
                           capture_output=True)
    if probe.returncode != 0:
        print("SKIP: python3-gi not installed")
        return 0

    addr = subprocess.run(["dbus-daemon", "--session", "--print-address",
                           "--fork"], capture_output=True, text=True,
                          check=True).stdout.strip()
    tmp = tempfile.mkdtemp()
    mac_file = os.path.join(tmp, "bt-headset")
    lock_file = os.path.join(tmp, "bt.lock")
    with open(mac_file, "w") as f:
        f.write(GO + "\n")

    env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=addr,
               TAPBOX_BT_FILE=mac_file, TAPBOX_BT_LOCKFILE=lock_file,
               TAPBOX_RECON_BOOT_RETRY="1", TAPBOX_RECON_BOOT_WINDOW="15",
               TAPBOX_RECON_BACKOFF_MIN="2", TAPBOX_RECON_BACKOFF_MAX="8",
               TAPBOX_RECON_DROP_RETRY="1", TAPBOX_RECON_DEBOUNCE="0.5",
               TAPBOX_RECON_LOCK_RETRY="1")

    def start_fake():
        p = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "tests", "fake_bluezd.py")],
            env=dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=addr),
            stdout=subprocess.PIPE, text=True)
        assert "ready" in p.stdout.readline()
        return p

    bus = dbus.bus.BusConnection(addr)

    def mock():
        return dbus.Interface(bus.get_object("org.bluez", "/org/tapbox/mock"),
                              "org.tapbox.Mock")

    fake = start_fake()
    mock().AddDevice(GO, "JBL GO", True, False, 0)

    daemon = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "pi", "btwatchd.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # 1: BOOT window connects the remembered, disconnected target
        wait_for("initial connect (BOOT)",
                 lambda: mock().GetConnected(GO), timeout=8)
        assert int(mock().GetConnectCount(GO)) == 1
        print("1. connect on start OK")

        # 2: target drops -> reconnect within seconds (signal, not poll)
        mock().SetConnected(GO, False)
        wait_for("reconnect after drop",
                 lambda: mock().GetConnected(GO), timeout=6)
        assert int(mock().GetConnectCount(GO)) == 2
        print("2. reconnect on drop OK")

        # 3: retarget via the MAC file -> new device connects fast
        mock().AddDevice(JR, "JBL JR310BT", True, False, 0)
        with open(mac_file, "w") as f:
            f.write(JR + "\n")
        wait_for("retarget connect", lambda: mock().GetConnected(JR),
                 timeout=6)
        print("3. instant retarget OK")

        # 4: bluez restart -> NameOwnerChanged re-enters the fast window
        fake.terminate()
        fake.wait(timeout=5)
        time.sleep(1)
        fake = start_fake()
        mock().AddDevice(JR, "JBL JR310BT", True, False, 0)
        wait_for("reconnect after bluez restart",
                 lambda: mock().GetConnected(JR), timeout=10)
        print("4. bluez-restart fast window OK")

        # 5: while another process holds the radio flock, the daemon must
        # NOT page — drop the target, hold the lock, watch nothing happen
        lock = open(lock_file, "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = int(mock().GetConnectCount(JR))
        mock().SetConnected(JR, False)
        time.sleep(3)
        assert int(mock().GetConnectCount(JR)) == before, \
            "daemon paged while the radio lock was held"
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        wait_for("reconnect after lock release",
                 lambda: mock().GetConnected(JR), timeout=6)
        print("5. flock deference OK")
    finally:
        daemon.terminate()
        out, _ = daemon.communicate(timeout=5)
        fake.terminate()
        if "--verbose" in sys.argv or os.environ.get("V"):
            print("--- daemon output ---\n" + out)

    print("C RECONNECT GATE OK — event-driven daemon behaves; "
          "safe to switch the unit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
