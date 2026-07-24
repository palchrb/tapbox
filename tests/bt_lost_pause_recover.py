#!/usr/bin/env python3
"""Gate the BT transport-loss contract (owner decision 2026-07-23):
when the headset is the chosen output there is NO local fail-over —
pause + bookmark + fastest automatic recovery + auto-resume, zero child
interaction. Replaces bt_lost_keep_playing.py (e81a53b reverted).

- transport lost mid-mpv: STOP (bookmark survives), never a live
  retarget to the built-in speaker — even with a card present and a
  live IPC; blip machinery armed.
- a heal probe spawns UNCONDITIONALLY on every bt-output loss (even
  idle) and self-discriminates inside _heal_crashed_controller.
- spotify: pause + lost_spotify armed.
- output != bt (incl. the hold-X park, which set_output couples to
  output=local): no-op, no heal.
- the heal itself: crash -> recover once + kick + CAS re-stamp of the
  resume window + cooldown reset on success; headset power-off -> no
  recover; a consumed 'lost' is NEVER re-stamped (no phantom resume,
  QA 2026-07-24); a FAILED recovery keeps the cooldown."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_LIBRARY"] = os.path.join(TMP, "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
REAL_HEAL = daemon._heal_crashed_controller  # before the spawn-stub below
daemon._bt.KICK_FILE = os.path.join(TMP, "bt-kick")
daemon.current_output = lambda **k: {"output": "bt"}
HEALS = []
daemon._heal_crashed_controller = lambda: HEALS.append(1)


def reset_wait():
    with daemon._BT_WAIT_LOCK:
        daemon._BT_WAIT.update(lost=0.0, since=0.0, ready_until=0.0,
                               lost_spotify=False)


# 1. THE REVERT PIN: mpv over bt, built-in card present, live IPC —
#    transport loss must STOP, never retarget to the box speaker
orch._mpv_alive = lambda: True
daemon._i2s_card_present = lambda: True
sent = []
daemon.mpv_ipc = lambda cmd: (sent.append(cmd), {"error": "success"})[1]
STOPPED = []
orch._stop_child = lambda: STOPPED.append(1)
reset_wait()
r = daemon._bt_transport_lost()
time.sleep(0.3)  # the heal spawn is a thread
assert r == {"stopped": True}, r
assert STOPPED == [1], "must stop the player"
assert not any(c[:2] == ["set_property", "audio-device"] for c in sent), \
    f"NEVER a live retarget to the built-in speaker: {sent}"
with daemon._BT_WAIT_LOCK:
    assert daemon._BT_WAIT["lost"] > 0, "blip auto-resume must arm"
assert HEALS == [1], "the heal probe must spawn on the loss"
print("1. mpv loss: stopped (no local fail-over), blip armed, heal spawned OK")

# 2. spotify mid-play: pause + lost_spotify, heal spawned
HEALS.clear()
orch._mpv_alive = lambda: False
daemon.spotify_playing = lambda *a, **k: True
GO = []
daemon.go = lambda path, **k: GO.append(path)
reset_wait()
r = daemon._bt_transport_lost()
time.sleep(0.3)
assert r == {"stopped": True} and GO == ["/player/pause"], (r, GO)
with daemon._BT_WAIT_LOCK:
    assert daemon._BT_WAIT["lost"] > 0 and daemon._BT_WAIT["lost_spotify"]
assert HEALS == [1]
print("2. spotify loss: paused, lost_spotify armed, heal spawned OK")

# 3. IDLE crash (nothing playing): heal STILL spawns — without it the
#    controller stays dead until the next button press
HEALS.clear()
daemon.spotify_playing = lambda *a, **k: False
reset_wait()
r = daemon._bt_transport_lost()
time.sleep(0.3)
assert r == {"stopped": False} and HEALS == [1], (r, HEALS)
print("3. idle loss: nothing stopped, heal still spawned OK")

# 4. output=local (a plain box, or the hold-X park — set_output couples
#    the quiet marker to local): full no-op, no heal
HEALS.clear()
daemon.current_output = lambda **k: {"output": "local"}
r = daemon._bt_transport_lost()
time.sleep(0.3)
assert r == {"stopped": False} and HEALS == [], (r, HEALS)
print("4. local output / hold-X park: no-op, no heal OK")

# 5. the heal: crash -> ONE recover, kick written, resume window CAS
#    re-stamped, cooldown RESET on success
daemon._bt._hci_crashed = lambda: True
RECOVERS = []
daemon._bt_recover = lambda verb: (RECOVERS.append(verb), True)[1]
daemon._BT_HEAL["last"] = 0.0
armed_at = time.monotonic() - 100
with daemon._BT_WAIT_LOCK:
    daemon._BT_WAIT["lost"] = armed_at  # armed 100s ago
REAL_HEAL()
assert RECOVERS == ["recover"], RECOVERS
assert os.path.exists(daemon._bt.KICK_FILE), "recovery must kick btwatchd"
with daemon._BT_WAIT_LOCK:
    assert daemon._BT_WAIT["lost"] > armed_at + 50, \
        "the resume window must re-base at recovery completion"
assert daemon._BT_HEAL["last"] == 0.0, \
    "a CLEAN recovery must reset the cooldown (flappy-evening re-crash)"
print("5. heal on crash: recover + kick + window re-based + cooldown reset OK")

# 5b. a consumed 'lost' (transport blipped back mid-heal) is NEVER
#     re-stamped — a phantom re-arm would fire a second resume/rebuild
#     under live audio
RECOVERS.clear()
with daemon._BT_WAIT_LOCK:
    daemon._BT_WAIT["lost"] = 0.0
REAL_HEAL()
assert RECOVERS == ["recover"]
with daemon._BT_WAIT_LOCK:
    assert daemon._BT_WAIT["lost"] == 0.0, \
        "a consumed 'lost' must stay consumed (CAS, no phantom resume)"
print("5b. consumed lost: never re-stamped by a finishing heal OK")

# 5c. headset power-off (no crash signature): the heal exits quietly —
#     reconnection is btwatchd's job
RECOVERS.clear()
daemon._bt._hci_crashed = lambda: False
daemon._BT_HEAL["last"] = 0.0
REAL_HEAL()
assert RECOVERS == [], "no recovery for a plain headset power-off"
print("5c. headset power-off: heal self-discriminates, no recovery OK")

# 5d. a FAILED recovery keeps the cooldown (the loop guard)
daemon._bt._hci_crashed = lambda: True
daemon._bt_recover = lambda verb: False
daemon._BT_HEAL["last"] = 0.0
REAL_HEAL()
assert daemon._BT_HEAL["last"] > 0, \
    "a failed recovery must keep the cooldown (no bluetooth-restart loop)"
before = daemon._BT_HEAL["last"]
daemon._bt_recover = lambda verb: (RECOVERS.append(verb), True)[1]
REAL_HEAL()  # within cooldown after the failure
assert RECOVERS == [], "cooldown must gate the retry after a failure"
assert daemon._BT_HEAL["last"] == before
print("5d. failed recovery: cooldown kept, retry gated OK")

print("\nall bt_lost_pause_recover checks passed")
