#!/usr/bin/env python3
"""Gate the output switch against an IN-FLIGHT spotify resume. Field
2026-07-18 18:01 (box booted with the speaker off): boot-resume loaded
Coco Del 4 PAUSED (play_spotify loads, seeks, then unpauses); btwatchd's
speaker-away fallback flipped output bt->local, set_output restarted
go-librespot — and the 'was playing' check missed the paused mid-resume
session, so nobody respawned. The waiting player timed out against the
dead session, resumed into an EMPTY new one (silent no-op) and exited:
the box came up mute. Contract: a LIVE spotify player child counts as
playback intent — the switch stops it and respawns with --exact."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
orch.source = "spotify"
orch.target = "https://open.spotify.com/album/2DBVACiT5O2z0aANPiOKyA"
daemon._kick_bt_connect = lambda: None
daemon._note_go_restart = lambda: None
daemon._i2s_card_present = lambda: True
daemon._bt_transport_ready = lambda: True
daemon._retarget_go_librespot = lambda pcm: True  # config differs -> restart

SPAWNED, STOPPED = [], []
orch._spawn = lambda target, **kw: SPAWNED.append((target, kw.get("exact")))
orch._stop_child = lambda: STOPPED.append(1)


class LiveChild:
    def poll(self):
        return None


def set_out(device, **kw):
    # current output = the opposite, so the switch is a real transition
    cur = "local" if device == "bt" else "bt"
    with open(daemon.OUT_FILE, "w") as f:
        json.dump({"output": cur, "pcm": f"tapbox_{cur}"}, f)
    return orch.set_output(device, **kw)


# 1. THE field crash: resume in flight (child alive, session paused so
# go_status shows not-playing) + speaker-away fallback to local -> the
# switch must stop the old player and respawn exactly
orch.child = LiveChild()
daemon.go_status = lambda **k: {"track": {"uri": "spotify:track:coco4"},
                                "paused": True, "stopped": False}
set_out("local", fallback=True)
assert STOPPED == [1], "old waiting player must be stopped first"
assert SPAWNED == [(orch.target, True)], \
    f"in-flight resume must survive the output switch: {SPAWNED}"
print("1. speaker-away flip mid-resume: player respawned exactly OK")

# 2. same protection when the speaker CONNECTS (local -> bt flip)
SPAWNED.clear()
STOPPED.clear()
set_out("bt", fallback=True)
assert SPAWNED == [(orch.target, True)], SPAWNED
print("2. speaker-connected flip mid-resume: respawned exactly OK")

# 3. go_status unreachable during the switch (wifi flap) must not crash
# set_output — the live child still proves intent
SPAWNED.clear()
STOPPED.clear()

def _boom(**_k):
    raise OSError("flap")

daemon.go_status = _boom
r = set_out("local", fallback=True)
assert SPAWNED == [(orch.target, True)], SPAWNED
assert r.get("output") == "local"
print("3. api unreachable during switch: no crash, resume carried OK")

# 4. idle spotify source (no child, nothing playing): switch restarts
# go-librespot but does NOT invent playback
SPAWNED.clear()
STOPPED.clear()
orch.child = None
daemon.go_status = lambda **k: {}
set_out("local", fallback=True)
assert SPAWNED == [], f"idle switch must not start music: {SPAWNED}"
print("4. idle switch: no surprise playback OK")

print("OUTPUT SWITCH RESUME OK — an in-flight spotify resume survives "
      "speaker-away/connected flips; idle stays idle.")
