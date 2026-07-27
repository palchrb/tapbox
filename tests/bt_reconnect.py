#!/usr/bin/env python3
"""Phase C gate (PLAN-bt-dbus.md §7): the event-driven reconnect daemon
(btwatchd) against fake-bluezd on a private bus. Verifies the event
paths the bash poll loop could only cover with a 60s worst case:

  1. connect on start (BOOT window)
  2. reconnect within seconds when the target drops
  3. instant retarget when the MAC file changes
  4. re-entry through the BOOT fast window after a bluez restart
  5. flock deference: no Connect while another process owns the radio
  6. follow-the-connector (owner request 2026-07-27): a paired audio
     sink that connects ITSELF is adopted via POST /bt/connect — but
     only while the configured speaker is absent, only for bonded
     audio sinks, never while the target is alive

Run ON THE RIG (or any machine with python3-dbus/python3-gi):
    python3 tests/bt_reconnect.py
SKIPs cleanly where dbus-daemon or the python bindings are missing.
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GO = "30:C0:1B:BD:13:B2"
JR = "2C:FD:B3:5B:1C:BA"
CAR = "B4:EC:02:4F:36:7C"
PHONE = "AA:BB:CC:DD:EE:FF"
SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"

# stub tapboxd: records POSTs so scenario 6 can see the adopt call
POSTS = []


class StubDaemon(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        POSTS.append((self.path, body))
        out = json.dumps({"ok": True, "unchanged": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


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

    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubDaemon)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=addr,
               TAPBOX_BT_FILE=mac_file, TAPBOX_BT_LOCKFILE=lock_file,
               TAPBOX_DAEMON=f"http://127.0.0.1:{stub.server_port}",
               TAPBOX_RECON_FALLBACK="1",
               TAPBOX_RECON_BOOT_RETRY="1", TAPBOX_RECON_BOOT_WINDOW="15",
               TAPBOX_RECON_BACKOFF_MIN="2", TAPBOX_RECON_BACKOFF_MAX="8",
               TAPBOX_RECON_DROP_RETRY="1", TAPBOX_RECON_DEBOUNCE="0.5",
               TAPBOX_RECON_LOCK_RETRY="1",
               TAPBOX_RECON_ADOPT_CONFIRM="0.5",
               TAPBOX_RECON_ADOPT_DEBOUNCE="2")

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

        # 6a: the car pages us while the TARGET IS ALIVE -> no adoption
        # (the Skoda connects itself on ignition even when the kid is on
        # the headset in the back seat — stealing the stream would be
        # the new bug). Also: a paired device WITHOUT the audio-sink
        # profile (a phone) never adopts, target alive or not.
        adopts = lambda: [p for p in POSTS if p[0] == "/bt/connect"]  # noqa: E731
        mock().AddDevice(CAR, "Skoda BT 4441", True, False, 0)
        mock().SetUuids(CAR, SINK_UUID)
        mock().AddDevice(PHONE, "Parent Phone", True, False, 0)
        mock().SetConnected(CAR, True)
        mock().SetConnected(PHONE, True)
        time.sleep(1.5)  # confirm delay is 0.5s — give it slack
        assert not adopts(), f"must not adopt: {POSTS}"
        print("6a. target alive / non-sink device: no adoption OK")

        # 6b: the target is genuinely away (pages fail) and the car
        # re-pages us -> adopted via POST /bt/connect
        mock().SetConnected(CAR, False)
        mock().SetConnectResult(JR, "failed")
        mock().SetConnected(JR, False)
        time.sleep(2.2)  # past the adopt debounce from 6a
        mock().SetConnected(CAR, True)
        wait_for("adopt POST /bt/connect",
                 lambda: any(b.get("mac") == CAR for _, b in adopts()),
                 timeout=6)
        assert len(adopts()) == 1, f"adopt must fire once: {POSTS}"
        print("6b. absent target + inbound paired sink: adopted OK")
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
