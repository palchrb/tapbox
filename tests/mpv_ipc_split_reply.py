#!/usr/bin/env python3
"""mpv's reply can arrive in the SECOND packet, and used to be lost.

ipc() did one recv() and scanned that chunk for the line carrying an
"error" field. But mpv broadcasts end-file / start-file /
playback-restart to every connected client, so during a track-change
storm the events fill the first packet and the reply lands in the next
one — and ipc() returned {}. That is precisely the condition its
callers exist for: the 15-episodes-in-3-seconds error storm that the
dead-output watchdog has to recognise (field 2026-07-17). The function
failed most reliably in the fault it was written for.

It now drains until the reply appears — but only for DRAIN_S, because
several callers hold the daemon's lock and a control press that cannot
take that lock within a second is dropped outright.

This is the first test to touch this module at all; every other test
monkeypatches daemon.mpv_ipc and never reaches the socket."""
import json
import os
import socket
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import mpv  # noqa: E402

EVENTS = [{"event": "end-file", "reason": "error"},
          {"event": "start-file"},
          {"event": "playback-restart"}]


def serve(path, packets, hold=0.0):
    """A fake mpv: emit each packet as its own send, then wait."""
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            conn.recv(65536)                      # the command
            for i, pkt in enumerate(packets):
                if i:
                    time.sleep(0.05)              # force separate reads
                conn.sendall(pkt)
            time.sleep(hold)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv, t


def enc(msgs):
    return b"".join(json.dumps(m).encode() + b"\n" for m in msgs)


# 1. the storm: three events first, the reply only in the second packet
path = os.path.join(TMP, "a.sock")
srv, t = serve(path, [enc(EVENTS),
                      enc([{"error": "success", "data": 42}])])
r = mpv.ipc(["get_property", "playlist-pos"], sock=path)
srv.close()
assert r == {"error": "success", "data": 42}, r
print("1. reply in the second packet is found, not dropped OK")

# 2. the same shape through get(), which is what callers actually use
path = os.path.join(TMP, "b.sock")
srv, t = serve(path, [enc(EVENTS), enc(EVENTS),
                      enc([{"error": "success", "data": False}])])
assert mpv.get("pause", sock=path) is False
srv.close()
print("2. get() sees through two packets of event noise OK")

# 3. a reply split ACROSS a packet boundary must still parse — the
#    partial line has to survive to the next read
path = os.path.join(TMP, "c.sock")
whole = enc([{"error": "success", "data": "abc"}])
srv, t = serve(path, [whole[:12], whole[12:]])
assert mpv.ipc(["get_property", "path"], sock=path) == {
    "error": "success", "data": "abc"}
srv.close()
print("3. a reply split mid-line is reassembled OK")

# 4. events but no reply: give up after DRAIN_S, NOT after `timeout`.
#    Callers hold a lock; a control press that waits a second is lost.
path = os.path.join(TMP, "d.sock")
srv, t = serve(path, [enc(EVENTS)], hold=3.0)
started = time.monotonic()
assert mpv.ipc(["set_property", "pause", True], sock=path, timeout=5) == {}
took = time.monotonic() - started
srv.close()
assert took < mpv.DRAIN_S + 1.0, f"drained for {took:.2f}s, too long"
print(f"4. no reply: gives up after ~{took:.2f}s, not the full timeout OK")

# 5. mpv closing the connection ends it immediately, no waiting
path = os.path.join(TMP, "e.sock")
srv, t = serve(path, [enc(EVENTS)])
started = time.monotonic()
assert mpv.ipc(["get_property", "pause"], sock=path) == {}
assert time.monotonic() - started < mpv.DRAIN_S + 0.6
srv.close()
print("5. a closed socket returns at once OK")

# 6. a dead socket still raises OSError — get() turns that into None,
#    which is what every caller's error handling is built on
try:
    mpv.ipc(["get_property", "pause"], sock=os.path.join(TMP, "nope.sock"))
    raise AssertionError("a missing socket must raise")
except OSError:
    pass
assert mpv.get("pause", sock=os.path.join(TMP, "nope.sock")) is None
print("6. missing socket: OSError from ipc, None from get OK")

print("\nMPV IPC OK — the reply is found even when mpv is shouting.")
