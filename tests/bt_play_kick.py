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
w, r = daemon._bt_wait_state(playing=False)
assert (w, r) == (True, False), (w, r)
print("8. play against a missing speaker -> bt_waiting OK")

# 9. transport shows up -> flips to bt_ready ("press play"), not waiting
TRANSPORT[0] = True
w, r = daemon._bt_wait_state(playing=False)
assert (w, r) == (False, True), (w, r)
w, r = daemon._bt_wait_state(playing=False)
assert (w, r) == (False, True), "ready must persist for its window"
print("9. transport up -> bt_ready popup OK")

# 10. they pressed play -> both popups gone
w, r = daemon._bt_wait_state(playing=True)
assert (w, r) == (False, False), (w, r)
w, r = daemon._bt_wait_state(playing=False)
assert (w, r) == (False, False), "ready must clear once play happened"
print("10. playing clears the popups OK")

# 11. stale intent (kid walked away) expires without ever flipping ready
TRANSPORT[0] = False
daemon.ORCH.command("playpause")
daemon._BT_WAIT["since"] = time.monotonic() - daemon.BT_WAIT_S - 1
w, r = daemon._bt_wait_state(playing=False)
assert (w, r) == (False, False), (w, r)
print("11. stale wait expires quietly OK")

print("BT PLAY KICK OK — pressing play connects the speaker now, "
      "not after the backoff, heals a crashed controller, and the "
      "screen knows what to say meanwhile.")
