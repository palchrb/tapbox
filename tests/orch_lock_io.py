#!/usr/bin/env python3
"""Gate review 2026-07-18 R2: the slow subprocess/systemctl calls must
not run while holding ORCH.lock, or every reader (the screen's 1/s
/status poll, _audible_now, the governor) queues behind seconds of I/O
— the frozen-UI class GO_STATUS_TIMEOUT was added to kill. Under test:
play()'s go-librespot ensure (systemctl is-active + up to 30s start)
runs before the lock; set_output's go-librespot retarget (systemctl
restart) runs after release, in both the real-switch and the deferred-
converge paths; and the observable behavior around them is unchanged —
offline play still fails fast, an in-flight resume still respawns, and
a fresh tap that lands mid-surgery is never stomped by the old
target's respawn."""
import json
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
SPOT = "https://open.spotify.com/album/2DBVACiT5O2z0aANPiOKyA"


def lock_is_free(grace=2.0):
    """Can WE take ORCH.lock right now? (A short timeout absorbs the
    arbiter's own sub-ms holds.)"""
    if orch.lock.acquire(timeout=grace):
        orch.lock.release()
        return True
    return False


SPAWNED, STOPPED = [], []
daemon.Orchestrator._spawn = (
    lambda self, target, *a, **kw: SPAWNED.append((target,
                                                   kw.get("exact"))))
daemon.Orchestrator._stop_child = lambda self: STOPPED.append(1)
daemon._kick_bt_connect = lambda: None


# --- play(): the backend ensure runs BEFORE the lock -----------------------

ENTERED = threading.Event()
GATE = threading.Event()
FREE_DURING = []


def blocking_ensure(self):
    FREE_DURING.append(lock_is_free())
    ENTERED.set()
    GATE.wait(10)
    return False  # parked + offline


daemon.Orchestrator._ensure_spotify_backend = blocking_ensure

result = []
t = threading.Thread(target=lambda: result.append(orch.play(SPOT)),
                     daemon=True)
t.start()
assert ENTERED.wait(5), "ensure never ran"

# 1. while the ensure blocks (a systemctl start mid-flight), the lock is
# free — /status readers never queue behind it
assert FREE_DURING == [True], "ensure held ORCH.lock"
assert lock_is_free(), "ORCH.lock held across the backend ensure"
print("1. play(): backend ensure leaves ORCH.lock free OK")

# 2. behavior kept: parked + offline still fails fast, spawns nothing
GATE.set()
t.join(5)
assert result and result[0].get("error") == "no-internet", result
assert not SPAWNED, f"offline play must not spawn: {SPAWNED}"
print("2. play(): parked+offline still errors fast, no zombie player OK")

# 3. behavior kept: backend fine -> the play proceeds to a spawn
daemon.Orchestrator._ensure_spotify_backend = lambda self: True
orch.child = None
r = orch.play(SPOT)
assert r.get("error") is None and SPAWNED == [(SPOT, None)], (r, SPAWNED)
print("3. play(): healthy backend still spawns OK")


# --- set_output(): the go-librespot restart runs AFTER release -------------

class LiveChild:
    def poll(self):
        return None


daemon._i2s_card_present = lambda: True
daemon._bt_transport_ready = lambda: True
daemon._note_go_restart = lambda: None
daemon.go_status = lambda **k: {}
daemon.mpv_get = lambda p: None


def set_out(device, **kw):
    cur = "local" if device == "bt" else "bt"
    with open(daemon.OUT_FILE, "w") as f:
        json.dump({"output": cur, "pcm": f"vibb_{cur}"}, f)
    return orch.set_output(device, **kw)


RETARGET_ENTERED = threading.Event()
RETARGET_GATE = threading.Event()


def blocking_retarget(pcm):
    FREE_DURING.append(lock_is_free())
    RETARGET_ENTERED.set()
    RETARGET_GATE.wait(10)
    return True  # config differed -> restarted


daemon._retarget_go_librespot = blocking_retarget

# 4. mid-restart (the systemctl seconds), the lock is free for readers
FREE_DURING.clear()
SPAWNED.clear()
STOPPED.clear()
orch.source, orch.target = "spotify", SPOT
orch.child = LiveChild()  # a resume in flight
result2 = []
t2 = threading.Thread(target=lambda: result2.append(set_out("local")),
                      daemon=True)
t2.start()
assert RETARGET_ENTERED.wait(5), "retarget never ran"
assert FREE_DURING == [True], "retarget held ORCH.lock"
assert lock_is_free(), "ORCH.lock held across the go-librespot restart"
print("4. set_output(): go-librespot restart leaves ORCH.lock free OK")

# 5. behavior kept: the in-flight resume still respawns with --exact
RETARGET_GATE.set()
t2.join(5)
assert result2 and result2[0].get("spotify_restarted") is True, result2
assert STOPPED and SPAWNED == [(SPOT, True)], (STOPPED, SPAWNED)
print("5. set_output(): in-flight resume still respawns exactly OK")

# 6. a fresh tap landing mid-surgery owns the child: the old target's
# respawn must NOT stomp it once the lock comes back
RETARGET_ENTERED.clear()
RETARGET_GATE.clear()
SPAWNED.clear()
STOPPED.clear()
orch.source, orch.target = "spotify", SPOT
orch.child = LiveChild()
t3 = threading.Thread(target=lambda: set_out("local"), daemon=True)
t3.start()
assert RETARGET_ENTERED.wait(5)
orch.target = "https://open.spotify.com/album/FRESHTAP111111111111"
RETARGET_GATE.set()
t3.join(5)
assert not SPAWNED, f"old target respawn stomped a fresh tap: {SPAWNED}"
print("6. set_output(): a mid-surgery fresh tap is never stomped OK")

# 7. deferred converge (fallback, output already = device): the
# idempotent retarget also runs without the lock
RETARGET_ENTERED.clear()
RETARGET_GATE.set()  # don't block this one
FREE_DURING.clear()
orch.child = None
with open(daemon.OUT_FILE, "w") as f:
    json.dump({"output": "bt", "pcm": "vibb_bt"}, f)
r = orch.set_output("bt", fallback=True)
assert r == {"unchanged": True, "output": "bt"}, r
assert FREE_DURING == [True], "deferred-converge retarget held ORCH.lock"
print("7. deferred converge retargets outside the lock OK")

print("ORCH LOCK IO OK — systemctl work no longer freezes the screen's "
      "poll; every switch/resume behavior survived the move.")
