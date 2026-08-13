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
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
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
RETARGETED = []
daemon._retarget_go_librespot = lambda pcm: (RETARGETED.append(pcm) or True)
# default: pre-v0.0.7 binary — the live reopen endpoint is absent, so
# every switch falls back to the config-rewrite + restart path
daemon.reopen_go_output = lambda pcm: False

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
        json.dump({"output": cur, "pcm": f"vibb_{cur}"}, f)
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

# 5. v0.0.7 LIVE reopen: the endpoint moves the output without tearing
# down the session, so an in-flight resume needs NO stop/respawn and NO
# restart — the music just keeps playing on the new device.
SPAWNED.clear()
STOPPED.clear()
RETARGETED.clear()
orch.child = LiveChild()
daemon.go_status = lambda **k: {"track": {"uri": "spotify:track:coco4"},
                               "paused": True, "stopped": False}
daemon.reopen_go_output = lambda pcm: True  # current binary
r = set_out("local", fallback=True)
assert STOPPED == [], f"live reopen keeps the session — no stop: {STOPPED}"
assert SPAWNED == [], f"live reopen keeps the session — no respawn: {SPAWNED}"
assert RETARGETED == [], f"live reopen must not restart go-librespot: {RETARGETED}"
assert r.get("spotify_restarted") is False
print("5. v0.0.7 live reopen: session kept, no restart, no respawn OK")

# 6. bt not ready: neither the live reopen NOR the restart may touch
# go-librespot — the wifi burst/reopen would land mid-AVDTP on the
# shared radio. The switch is deferred to btwatchd's announce.
SPAWNED.clear()
STOPPED.clear()
RETARGETED.clear()
REOPENED = []
daemon.reopen_go_output = lambda pcm: (REOPENED.append(pcm) or True)
daemon._bt_transport_ready = lambda: False
orch.child = LiveChild()
r = set_out("bt", fallback=True)
assert REOPENED == [], f"no reopen onto a transport-less speaker: {REOPENED}"
assert RETARGETED == [], f"no restart onto a transport-less speaker: {RETARGETED}"
assert SPAWNED == [] and STOPPED == []
assert r.get("spotify_restarted") is False
daemon._bt_transport_ready = lambda: True  # restore
print("6. bt not ready: go-librespot untouched, switch deferred OK")

print("OUTPUT SWITCH RESUME OK — an in-flight spotify resume survives "
      "speaker-away/connected flips (v0.0.7 live reopen keeps it playing "
      "with no restart); idle stays idle.")
