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
# --- a play attempt against a missing speaker must SAY so, then just
# --- RESUME when the transport shows up within the blip window --------------

CRASHED[0] = False
TRANSPORT = [False]
daemon._bt_transport_ready = lambda: TRANSPORT[0]
ALIVE = [False]
daemon.Orchestrator._mpv_alive = lambda self: ALIVE[0]
STOPPED, SPAWNED = [], []
daemon.Orchestrator._stop_child = (
    lambda self: (STOPPED.append(1), ALIVE.__setitem__(0, False)))
daemon.Orchestrator._spawn = (
    lambda self, target, **kw: SPAWNED.append(target))
daemon.ORCH.target = "https://feeds.example.com/show"
daemon.ORCH.source = "mpv"


def wait_spawn(n):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(SPAWNED) >= n:
            return
        time.sleep(0.02)
    raise SystemExit(f"TIMEOUT waiting for auto-resume ({SPAWNED})")


def arm_waiting(age=0.0):
    daemon._BT_WAIT.update(since=time.monotonic() - age, lost=0.0,
                           ready_until=0.0)


# 8. play intent with the speaker away -> bt_waiting popup
set_output("bt")
TRANSPORT[0] = False
arm_waiting()
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (True, False, False), (w, r, l)
print("8. play against a missing speaker -> bt_waiting OK")

# 9. speaker connects WITHIN the window -> auto-resume, no 'press A'
SPAWNED.clear()
ALIVE[0] = False
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, False), (w, r, l)
wait_spawn(1)
print("9. waiting popup: speaker back in window -> auto-play, no A OK")

# 9b. speaker connects LATE -> press-A flash, no surprise auto-resume
SPAWNED.clear()
arm_waiting(age=daemon.BT_RESUME_S + 1)
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, True, False), (w, r, l)
time.sleep(0.2)
assert not SPAWNED, f"late connect must NOT auto-resume: {SPAWNED}"
print("9b. waiting popup: late connect -> press-A, no surprise audio OK")
daemon._BT_WAIT["ready_until"] = 0.0

# 9c. you switched the output to the BT speaker but it's not connected,
# so audio keeps coming from the built-in one (playing=True). The
# 'not connected' popup must STAY (X connects, A drops back to local) —
# it only clears once the output is local or the transport is up.
arm_waiting()
set_output("bt")
TRANSPORT[0] = False
w, r, l = daemon._bt_wait_state(playing=True)
assert (w, r, l) == (True, False, False), \
    "output-switch-to-bt popup must persist while playing on built-in"
# ...dropping the output back to local clears it
set_output("local")
w, r, l = daemon._bt_wait_state(playing=True)
assert (w, r, l) == (False, False, False), (w, r, l)
# ...and so does the transport coming up (audio moves to bt)
arm_waiting()
set_output("bt")
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=True)
assert (w, r, l) == (False, False, False), (w, r, l)
TRANSPORT[0] = False
set_output("bt")
print("9c. output-switch-to-bt popup persists on built-in, clears on "
      "local/connect OK")

# 10. playback is on -> popups gone
w, r, l = daemon._bt_wait_state(playing=True)
assert (w, r, l) == (False, False, False), (w, r, l)
print("10. playing clears the popups OK")

# 11. stale intent (kid walked away) expires without ever flipping ready
TRANSPORT[0] = False
arm_waiting(age=daemon.BT_WAIT_S + 1)
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, False), (w, r, l)
print("11. stale wait expires quietly OK")


# --- the speaker DIED mid-play (btwatchd's /bt/lost hint): stop the
# --- player before mpv error-skips through the queue (field log
# --- 2026-07-17: ~15 episodes in 3s), then resume/offer the choice ---------

# 12. playing on bt + transport dies -> player stopped, bt_lost armed
ALIVE[0] = True
r12 = daemon._bt_transport_lost()
assert r12 == {"stopped": True} and STOPPED, r12
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, True), (w, r, l)
print("12. transport death mid-play stops the player + arms bt_lost OK")

# 13. speaker back within the blip window -> resumes BY ITSELF, no
# popup homework (a short dropout is the code's problem, not the kid's)
SPAWNED.clear()
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, False), (w, r, l)
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

# 16. spotify plays via go-librespot (no mpv child): the lost hint
# PAUSES it — its ALSA output just died under it — and arms the popup
GO_CALLS = []
daemon.go = lambda path, **kw: GO_CALLS.append(path)
daemon.spotify_playing = lambda: True
ALIVE[0] = False
w, r, l = daemon._bt_wait_state(playing=False)  # drain scenario 15 state
r16 = daemon._bt_transport_lost()
assert r16 == {"stopped": True}, r16
assert GO_CALLS == ["/player/pause"], GO_CALLS
w, r, l = daemon._bt_wait_state(playing=False)
assert l is True, (w, r, l)
print("16. spotify over bt: lost hint pauses it + arms the popup OK")

# 17. blip on spotify: go-librespot's ALSA output died WITH the
# transport — a plain /player/resume plays SILENTLY (field log 19:21).
# The blip must REBUILD the output (restart) and replay via the spawn
# path, never a bare resume.
GO_CALLS.clear()
REBUILT = []
_REAL_REBUILD = daemon._go_output_rebuild
daemon._go_output_rebuild = lambda: REBUILT.append(1)
daemon.ORCH.source = "spotify"
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, False), (w, r, l)
deadline = time.monotonic() + 5
while time.monotonic() < deadline and len(SPAWNED) < 2:
    time.sleep(0.02)
assert REBUILT == [1], f"spotify blip must rebuild the output: {REBUILT}"
assert len(SPAWNED) == 2, f"spotify blip must replay via spawn: {SPAWNED}"
assert "/player/resume" not in GO_CALLS, \
    f"bare resume plays silently into the dead handle: {GO_CALLS}"
TRANSPORT[0] = False
print("17. blip on spotify -> output rebuild + replay, no bare resume OK")

# 17b. spotify + LATE return: rebuild fires so the kid's press-A lands
# on a fresh output; ready flash shows; nothing auto-plays
REBUILT.clear()
GO_CALLS.clear()
daemon._bt_transport_lost()  # spotify branch (spotify_playing True)
assert GO_CALLS == ["/player/pause"], GO_CALLS
daemon._BT_WAIT["lost"] = time.monotonic() - daemon.BT_RESUME_S - 1
TRANSPORT[0] = True
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, True, False), (w, r, l)
deadline = time.monotonic() + 5
while time.monotonic() < deadline and not REBUILT:
    time.sleep(0.02)
assert REBUILT == [1], "late spotify return must still rebuild the output"
time.sleep(0.2)
assert len(SPAWNED) == 2, f"late return must NOT auto-play: {SPAWNED}"
daemon.ORCH.source = "mpv"
TRANSPORT[0] = False
daemon.spotify_playing = lambda: False
daemon._BT_WAIT["ready_until"] = 0.0
print("17b. late spotify return -> rebuild for press-A, no surprise audio OK")

# 18. the automatic fallback flipping output to local must NOT disarm
# the popup/auto-resume (it did: ~23s after every drop on HAT boxes,
# which silently killed the auto-resume promise)
ALIVE[0] = True
set_output("bt")
daemon._bt_transport_lost()
set_output("local")  # follow-the-speaker fallback flips the output
w, r, l = daemon._bt_wait_state(playing=False)
assert l is True, "fallback output flip disarmed the lost popup"
TRANSPORT[0] = True  # speaker back — auto-resume must still be armed
w, r, l = daemon._bt_wait_state(playing=False)
assert (w, r, l) == (False, False, False), (w, r, l)
wait_spawn(3)
print("18. output fallback keeps popup + auto-resume armed OK")

# 19. the auto-resume must fire WITHOUT any /status poll — the screen
# sleeps and stops polling, so the daemon's background watcher tick has
# to drive it (field: playback didn't start until a button woke the
# screen). Drive it via _bt_wait_advance alone (what the watcher calls),
# never touching _bt_wait_state/status.
daemon.ORCH.source = "mpv"
daemon.spotify_playing = lambda: False
set_output("bt")
SPAWNED.clear()
TRANSPORT[0] = False
ALIVE[0] = True
daemon._bt_transport_lost()   # arm lost (mpv playing over bt)
ALIVE[0] = False              # daemon stopped the child
TRANSPORT[0] = True           # speaker comes back while the screen is dark
daemon._bt_wait_advance()     # ONLY the watcher's tick — no status() call
wait_spawn(1)
assert not daemon._BT_WAIT["lost"], "watcher tick did not clear the lost state"
print("19. background watcher auto-resumes with the screen asleep "
      "(no /status poll) OK")

# 20. the watcher THREAD itself: arm a wait, let a real tick fire it
daemon.BT_WAIT_TICK_S = 0.05
import threading as _t  # noqa: E402
_t.Thread(target=daemon._bt_wait_watcher, daemon=True).start()
set_output("bt")
SPAWNED.clear()
TRANSPORT[0] = False
ALIVE[0] = True
daemon._bt_transport_lost()
ALIVE[0] = False
TRANSPORT[0] = True
wait_spawn(1)  # the thread's tick fires the resume on its own
print("20. the watcher thread fires the resume on its own OK")

# 21. ONE reconnect = ONE resume. Switching output to bt arms 'since';
# the link then blips mid-setup and arms 'lost'. Both pending, the
# speaker returns -> _speaker_back must fire ONCE, not once per intent
# (twice = two go-librespot restarts racing, the field storm 23:07).
_real_speaker_back = daemon._speaker_back
SB_CALLS = []
daemon._speaker_back = lambda now, elapsed, spot: (SB_CALLS.append(spot)
                                                   or 0.0)
daemon._BT_WAIT.update(lost=0.0, since=0.0, ready_until=0.0)
daemon._BT_WAIT["since"] = time.monotonic()          # output switched to bt
daemon._BT_WAIT["lost"] = time.monotonic()           # then a mid-setup blip
daemon._BT_WAIT["lost_spotify"] = True
TRANSPORT[0] = True
daemon._bt_wait_advance()
assert SB_CALLS == [True], f"one reconnect must resume once: {SB_CALLS}"
assert not daemon._BT_WAIT["lost"] and not daemon._BT_WAIT["since"], \
    "both intents must clear on the single resume"
daemon._speaker_back = _real_speaker_back
TRANSPORT[0] = False
print("21. lost+since pending: the reconnect resumes exactly once OK")

# 22. the go-librespot rebuild cooldown: a retarget restart followed by a
# blip rebuild on the SAME reconnect must restart the service only once
REST = []
daemon._go_output_rebuild = _REAL_REBUILD          # undo scenario 17's stub
daemon.subprocess.run = lambda *a, **k: REST.append(a[0])
daemon.go_status = lambda **k: {"username": "pa"}
daemon._GO_REBUILD["at"] = 0.0
daemon._note_go_restart()          # the output retarget just restarted it
daemon._go_output_rebuild()        # blip rebuild moments later -> skip
assert REST == [], f"rebuild within cooldown must NOT restart again: {REST}"
daemon._GO_REBUILD["at"] = time.monotonic() - daemon.GO_REBUILD_COOLDOWN_S - 1
daemon._go_output_rebuild()        # cooldown elapsed -> a real restart
assert len(REST) == 1 and "restart" in REST[0], \
    f"a rebuild past the cooldown must restart: {REST}"
print("22. go-librespot restarts collapse to one per reconnect OK")

print("BT PLAY KICK OK — pressing play connects the speaker now, "
      "not after the backoff, heals a crashed controller, the screen "
      "knows what to say, resumes even while asleep, and no longer "
      "restart-storms go-librespot on a flapping speaker.")
