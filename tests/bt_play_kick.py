#!/usr/bin/env python3
"""Gate the play-intent BT kick: pressing play (or any transport button)
while the configured output is a disconnected BT speaker must poke
btwatchd's kick file — an immediate connect attempt — instead of leaving
the kid waiting out the 20->300s blind-retry backoff after a boot where
the speaker came on late. No kick when the speaker is connected, and
never on the built-in output."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = STATE
os.environ["TAPBOX_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ.setdefault("TAPBOX_CACHE", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

KICK = daemon._bt.KICK_FILE


def set_output(device):
    with open(daemon.OUT_FILE, "w") as f:
        json.dump({"output": device, "pcm": "x"}, f)


def kicked():
    hit = os.path.exists(KICK)
    if hit:
        os.remove(KICK)
    return hit


# output = bt, speaker NOT connected
set_output("bt")
daemon._bt_transport_ready = lambda: False

# 1. a transport button kicks btwatchd (even with nothing to control)
daemon.ORCH.command("playpause")
assert kicked(), "playpause did not kick btwatchd"
print("1. playpause with disconnected speaker kicks a connect OK")

# 2. /play kicks too (spawn stubbed out — no real player process)
daemon.Orchestrator._spawn = lambda self, *a, **k: None
daemon.Orchestrator._stop_child = lambda self: None
daemon.ORCH.play("https://feeds.example.com/show")
assert kicked(), "play did not kick btwatchd"
print("2. play with disconnected speaker kicks a connect OK")

# 3. speaker already connected -> no kick (no churn on the radio)
daemon._bt_transport_ready = lambda: True
daemon.ORCH.command("playpause")
assert not kicked(), "kicked although the transport is up"
print("3. connected speaker is never kicked OK")

# 4. built-in output -> no kick, regardless of transport state
set_output("local")
daemon._bt_transport_ready = lambda: False
daemon.ORCH.command("playpause")
assert not kicked(), "kicked on the built-in output"
print("4. built-in output never kicks OK")


# --- crash self-heal: a kick can't fix a dead firmware (field log
# --- 2026-07-17: 'hardware error 0x00' left the speaker dead for good —
# --- btwatchd is passive by design and the stall watchdog never saw a
# --- stall once playback fell back to the local output) ---------------------

import time  # noqa: E402


def wait_for(what, pred, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise SystemExit(f"TIMEOUT waiting for: {what}")


CRASHED = [False]
RECOVERED = []
daemon._bt._hci_crashed = lambda: CRASHED[0]
daemon._bt_recover = lambda verb: RECOVERED.append(verb)
set_output("bt")

# 5. healthy controller: play intent never runs recovery
daemon.ORCH.command("playpause")
time.sleep(0.5)  # give the async heal check time to conclude
assert RECOVERED == [], "recovery ran on a healthy controller"
print("5. healthy controller: kick only, no recovery OK")

# 6. crash signature: exactly one recovery despite button mashing,
# and btwatchd gets re-kicked after it
CRASHED[0] = True
kicked()  # clear the kick file so the re-kick is observable
for _ in range(5):
    daemon.ORCH.command("playpause")
wait_for("crash recovery", lambda: RECOVERED)
time.sleep(0.5)  # the other presses' heal threads must all conclude
assert RECOVERED == ["recover"], f"recovery must run ONCE: {RECOVERED}"
wait_for("re-kick after recovery", kicked)
print("6. crashed controller: one recovery per cooldown + re-kick OK")

# 7. still crashed within the cooldown: no second recovery; after the
# cooldown expires a new crash is healed again
daemon.ORCH.command("playpause")
time.sleep(0.5)
assert RECOVERED == ["recover"], "cooldown did not hold"
daemon._BT_HEAL["last"] = 0.0  # cooldown over
daemon.ORCH.command("playpause")
wait_for("second recovery after cooldown", lambda: len(RECOVERED) == 2)
print("7. cooldown gates retries; a later crash heals again OK")

# --- the screen popups' state machine (/status bt_waiting/bt_ready):
# --- a play attempt against a missing speaker must SAY so, and say
# --- "press play" the moment the transport shows up ------------------------

CRASHED[0] = False
TRANSPORT = [False]
daemon._bt_transport_ready = lambda: TRANSPORT[0]

# 8. play intent with the speaker away -> bt_waiting
set_output("bt")
daemon.ORCH.command("playpause")
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (True, False, False), (w, r, l)
print("8. play against a missing speaker -> bt_waiting OK")

# 9. transport shows up -> flips to bt_ready ("press play"), not waiting
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, True, False), (w, r, l)
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, True, False), "ready must persist its window"
print("9. transport up -> bt_ready popup OK")

# 10. they pressed play -> both popups gone
w, r, l = daemon._bt_wait_state(playing=True)
assert (w, r, l) == (False, False, False), (w, r, l)
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, False), "ready must clear after play"
print("10. playing clears the popups OK")

# 11. stale intent (kid walked away) expires without ever flipping ready
TRANSPORT[0] = False
daemon.ORCH.command("playpause")
daemon._BT_WAIT["since"] = time.monotonic() - daemon.BT_WAIT_S - 1
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, False), (w, r, l)
print("11. stale wait expires quietly OK")


# --- the speaker DIED mid-play (btwatchd's /bt/lost hint): stop the
# --- player before mpv error-skips through the queue (field log
# --- 2026-07-17: ~15 episodes in 3s), then offer the choice ---------------

ALIVE = [False]
daemon.Orchestrator._mpv_alive = lambda self: ALIVE[0]
STOPPED, SPAWNED = [], []
daemon.Orchestrator._stop_child = (
    lambda self: (STOPPED.append(1), ALIVE.__setitem__(0, False)))
daemon.Orchestrator._spawn = (
    lambda self, target, **kw: SPAWNED.append(target))

# 12. playing on bt + transport dies -> player stopped, bt_lost armed
ALIVE[0] = True
r12 = daemon._bt_transport_lost()
assert r12 == {"stopped": True} and STOPPED, r12
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, True), (w, r, l)
print("12. transport death mid-play stops the player + arms bt_lost OK")

# 13. speaker back within the blip window -> resumes BY ITSELF, no
# popup homework (a short dropout is the code's problem, not the kid's)
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, False), (w, r, l)


def wait_spawn(n):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(SPAWNED) >= n:
            return
        time.sleep(0.02)
    raise SystemExit(f"TIMEOUT waiting for auto-resume ({SPAWNED})")


wait_spawn(1)
print("13. blip: speaker back quickly -> auto-resume, no popup OK")

# 13b. speaker back LATE -> "press A to play" flash, no auto-resume
TRANSPORT[0] = False
ALIVE[0] = True
daemon._bt_transport_lost()
daemon._BT_WAIT["lost"] = time.monotonic() - daemon.BT_RESUME_S - 1
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, True, False), (w, r, l)
time.sleep(0.3)
assert len(SPAWNED) == 1, f"late return must NOT auto-resume: {SPAWNED}"
print("13b. late return -> press-A flash, no surprise audio OK")
TRANSPORT[0] = False
daemon._BT_WAIT["ready_until"] = 0.0

# 14. guards: local output or no player -> a (stale) hint is a no-op
TRANSPORT[0] = False
STOPPED.clear()
set_output("local")
ALIVE[0] = True
assert daemon._bt_transport_lost() == {"stopped": False} and not STOPPED
set_output("bt")
ALIVE[0] = False
assert daemon._bt_transport_lost() == {"stopped": False} and not STOPPED
print("14. lost hint never touches local playback or a dead player OK")

# 15. resuming playback (any route) clears bt_lost
ALIVE[0] = True
daemon._bt_transport_lost()
w, r, l = daemon._bt_wait_state(playing=True)
assert (w, r, l) == (False, False, False), (w, r, l)
print("15. playing clears bt_lost OK")

print("BT PLAY KICK OK — pressing play connects the speaker now, "
      "not after the backoff, heals a crashed controller, and the "
      "screen knows what to say meanwhile.")
