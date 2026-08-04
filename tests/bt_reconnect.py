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
  7. AVDTP refusal (field 2026-08-04): a peer that accepts every page
     but never lets the audio channel up climbs the ladder and PARKS
     instead of looping every 3s; a kick still revives one attempt

Devices carry a PCM (SetPcm) wherever a connect should count as a real
success: without it, every 'connected' here is ACL-only — exactly the
refusal topology — and scenarios 1-5 would silently exercise the
escalation path instead of the blip fast path they exist to pin.

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
    kick_file = os.path.join(tmp, "bt-connect-kick")
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
               TAPBOX_BT_KICK=kick_file,
               TAPBOX_RECON_REFUSAL_PARK="3",
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
    mock().SetPcm(GO, True)  # a real speaker: audio follows the ACL

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
        mock().SetPcm(JR, True)
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
        mock().SetPcm(JR, True)  # the fake restart wiped the PCM table
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

        # 6a: the car pages us while the TARGET IS ALIVE -> no adoption,
        # and the newcomer's parallel link is politely KICKED (owner
        # 2026-07-27: never car + headset connected at once — a second
        # ACL carrying AVRCP polls during live A2DP is the crash dose).
        # A paired device WITHOUT the audio-sink profile (a phone) is
        # neither adopted nor kicked.
        adopts = lambda: [p for p in POSTS if p[0] == "/bt/connect"]  # noqa: E731
        mock().AddDevice(CAR, "Skoda BT 4441", True, False, 0)
        mock().SetUuids(CAR, SINK_UUID)
        mock().AddDevice(PHONE, "Parent Phone", True, False, 0)
        mock().SetConnected(CAR, True)
        mock().SetConnected(PHONE, True)
        wait_for("polite kick of the second sink",
                 lambda: not mock().GetConnected(CAR), timeout=6)
        time.sleep(1)  # give a would-be adopt POST time to appear
        assert not adopts(), f"must not adopt: {POSTS}"
        assert mock().GetConnected(PHONE), "a phone must be left alone"
        assert mock().GetConnected(JR), "the target must stay connected"
        print("6a. target alive: second sink kicked, no adoption, "
              "phone untouched OK")

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

        # 7: AVDTP refusal end-to-end — the peer accepts every page but
        # audio never appears (no PCM), and we drop the link right after
        # each connect, like the Skoda with CarPlay holding its slot.
        # REFUSAL_PARK=3 (env): the third refusal parks the pages.
        mock().SetConnected(CAR, False)     # quiet the adopted car
        mock().SetConnectResult(JR, "ok")   # pages succeed again
        mock().SetPcm(JR, False)            # ...but audio never comes up
        with open(kick_file, "w") as f:     # user intent: fresh window
            f.write("x")
        for i in range(3):
            wait_for(f"refusal connect {i + 1}",
                     lambda: mock().GetConnected(JR), timeout=25)
            mock().SetConnected(JR, False)  # AVDTP refused -> link drops
        count = int(mock().GetConnectCount(JR))
        time.sleep(9)  # > BACKOFF_MAX (8s in this env) + margin
        assert int(mock().GetConnectCount(JR)) == count, \
            "a refusal-parked target must not be paged"
        print("7a. three ACL-only connects park the pages OK")
        with open(kick_file, "w") as f:     # play press while parked
            f.write("x")
        wait_for("kick revival",
                 lambda: int(mock().GetConnectCount(JR)) == count + 1,
                 timeout=6)
        print("7b. a kick still revives exactly one attempt OK")
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
