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
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
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
# a CLEAN recovery backs off to the SHORT success cooldown (not the full
# 5min, not zero): the second crash of a flappy evening heals soon, but a
# re-crash can't loop driver reloads back-to-back
since_last = time.monotonic() - daemon._BT_HEAL["last"]
remaining = daemon.BT_HEAL_COOLDOWN_S - since_last
assert 0 < remaining <= daemon.BT_HEAL_SUCCESS_COOLDOWN_S + 5, \
    f"clean recovery must leave only the short cooldown, {remaining}s left"
print("5. heal on crash: recover + kick + window re-based + short cooldown OK")

# 5a. within the short success cooldown a re-crash is gated (no back-to-
#     back driver reloads); past it, it heals
RECOVERS.clear()
REAL_HEAL()  # immediately again — still inside the success cooldown
assert RECOVERS == [], "a re-crash inside the success cooldown must gate"
daemon._BT_HEAL["last"] = time.monotonic() - daemon.BT_HEAL_COOLDOWN_S - 1
with daemon._BT_WAIT_LOCK:
    daemon._BT_WAIT["lost"] = time.monotonic() - 10
REAL_HEAL()
assert RECOVERS == ["recover"], "past the success cooldown it heals again"
print("5a. success cooldown gates a re-crash, then heals OK")

# 5b. a consumed 'lost' (transport blipped back mid-heal) is NEVER
#     re-stamped — a phantom re-arm would fire a second resume/rebuild
#     under live audio
RECOVERS.clear()
daemon._BT_HEAL["last"] = 0.0
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

# 5e. a crash INSIDE the cooldown arms exactly ONE delayed re-probe
#     (field 2026-07-27 20:09: the third crash of the evening landed in
#     the 45s success cooldown with nobody pressing play — the
#     controller looped hardware errors until a manual reboot). The
#     re-probe re-enters the heal after expiry; a phantom (signature
#     gone) or a healthy gated call arms nothing.
RECHECKS = []
daemon._schedule_heal_recheck = lambda delay: RECHECKS.append(delay)
daemon._BT_HEAL["recheck"] = False
daemon._bt._hci_crashed = lambda: True
daemon._bt_recover = lambda verb: (RECOVERS.append(verb), True)[1]
daemon._BT_HEAL["last"] = time.monotonic() - 10  # deep inside cooldown
RECOVERS.clear()
REAL_HEAL()
assert RECOVERS == [] and len(RECHECKS) == 1, (RECOVERS, RECHECKS)
assert daemon._BT_HEAL["recheck"], "the armed flag must be set"
remaining = daemon.BT_HEAL_COOLDOWN_S - 10
assert remaining < RECHECKS[0] < remaining + 10, \
    f"re-probe must land just past cooldown expiry, got {RECHECKS[0]}"
REAL_HEAL()  # second crash signal while armed
assert len(RECHECKS) == 1, "re-probes must never stack"
# the probe fires: flag clears, cooldown expired -> a real recover runs
daemon._BT_HEAL["recheck"] = False  # what _fire() does first
daemon._BT_HEAL["last"] = time.monotonic() - daemon.BT_HEAL_COOLDOWN_S - 1
with daemon._BT_WAIT_LOCK:
    daemon._BT_WAIT["lost"] = time.monotonic() - 10
REAL_HEAL()
assert RECOVERS == ["recover"], RECOVERS
# a gated call WITHOUT the crash signature must not arm anything
RECHECKS.clear()
daemon._bt._hci_crashed = lambda: False
daemon._BT_HEAL["last"] = time.monotonic() - 10
REAL_HEAL()
assert RECHECKS == [], "healthy controller: no re-probe scheduled"
print("5e. crash inside cooldown: one delayed re-probe, then heals OK")

# 5f. a CLEAN probe (no signature yet) arms one silent re-probe — the
#     command-timeout wedge signature matures ~50s after the
#     transport-died notify (field 2026-07-30 12:14: the kernel killed
#     the stalled car link, the probe ran with ONE timeout in the
#     journal, the third came at +50s, and with output fallen back to
#     local nothing ever probed again — wedged until reboot). The
#     re-probe itself (rearm=False) must never chain, or every plain
#     headset power-off would tick probes forever.
RECHECKS.clear()
daemon._BT_HEAL["recheck"] = False
daemon._bt._hci_crashed = lambda: False
daemon._BT_HEAL["last"] = 0.0  # far past any cooldown
REAL_HEAL()
assert RECHECKS == [daemon.BT_HEAL_REPROBE_S], \
    f"a clean probe must arm one re-probe: {RECHECKS}"
assert daemon._BT_HEAL["recheck"], "the armed flag must be set"
REAL_HEAL()  # second clean trigger while armed
assert len(RECHECKS) == 1, "re-probes must never stack"
daemon._BT_HEAL["recheck"] = False  # what _fire() does first
REAL_HEAL(rearm=False)  # the re-probe fires, still clean
assert len(RECHECKS) == 1, "a clean re-probe must not chain another"
# ...and when the signature HAS matured by fire time, it heals
daemon._bt._hci_crashed = lambda: True
RECOVERS.clear()
REAL_HEAL(rearm=False)
assert RECOVERS == ["recover"], "a matured signature heals on the re-probe"
print("5f. clean probe arms one re-probe, no chaining, matured wedge heals OK")

print("\nall bt_lost_pause_recover checks passed")
