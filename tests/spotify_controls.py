#!/usr/bin/env python3
"""Gate the control path's behavior against a SLOW go-librespot. Field
2026-07-18 15:43-15:44 (rapid next/prev on an album): each press held the
control lock through multi-second API calls, queued presses fired late
and out of order, a /next whose status probe timed out fell through to
the replay fallback and RESTARTED the album from 0:00, and the
supervisor parked the busy-but-alive daemon. Contract now:
- presses arriving while a control runs are DROPPED (busy), not queued
- an unreachable-but-running go-librespot drops the press, never replays
- next/prev NEVER trigger the replay fallback — only playpause does"""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
daemon._kick_bt_connect = lambda: None
orch._mpv_alive = lambda: False
orch.source = "spotify"
orch.target = "https://open.spotify.com/album/4rxfprnLYz3592ZGaeqcON"
SPAWNED = []
orch._spawn = lambda target, **kw: SPAWNED.append(target)
orch._ensure_spotify_backend = lambda: True
CMDS = []
daemon.spotify_command = lambda a: CMDS.append(a)


def _unreachable(**_k):
    raise OSError("timed out")


# 1. api unreachable + unit RUNNING: next/prev/playpause all drop as
# busy — no replay, no spawn, no fallthrough
daemon.go_status = _unreachable
daemon._go_unit_active = lambda: True
for a in ("next", "prev", "playpause"):
    r = orch.command(a)
    assert r.get("busy") is True, (a, r)
assert SPAWNED == [] and CMDS == [], (SPAWNED, CMDS)
print("1. busy api (unit running): presses dropped, never a replay OK")

# 2. api unreachable + unit DOWN: playpause replays the target (bring
# the music back), next/prev do NOT (a skip must never restart an album)
daemon._go_unit_active = lambda: False
r = orch.command("next")
assert r["routed"] is None and not SPAWNED, (r, SPAWNED)
r = orch.command("prev")
assert r["routed"] is None and not SPAWNED, (r, SPAWNED)
r = orch.command("playpause")
assert r["routed"] == "resume" and SPAWNED == [orch.target], (r, SPAWNED)
print("2. dead session: playpause replays, next/prev stay dead buttons OK")

# 2b. album ran off its end: the API ANSWERS and says the session is
# empty — next must WRAP (replay the target) instead of going dead
# (field 2026-07-18: next on the last Coco track showed nothing at all)
SPAWNED.clear()
daemon.go_status = lambda **k: {}          # reachable, cleanly empty
r = orch.command("next")
assert r["routed"] == "resume" and SPAWNED == [orch.target], (r, SPAWNED)
print("2b. clean empty session: next wraps (replays the target) OK")

# 2c. a finished mpv queue replays on next too (go-librespot state is
# irrelevant for a podcast box — even unreachable must not block it)
SPAWNED.clear()
orch.source = "mpv"
daemon.go_status = _unreachable
r = orch.command("next")
assert r["routed"] == "resume" and SPAWNED == [orch.target], (r, SPAWNED)
orch.source = "spotify"
print("2c. finished podcast queue: next replays regardless of spotify OK")

# 3. loaded session: everything routes to spotify as before
SPAWNED.clear()
daemon.go_status = lambda **k: {"track": {"uri": "spotify:track:x"},
                                "paused": False, "stopped": False,
                                "play_origin": "go-librespot"}
r = orch.command("next")
assert r["routed"] == "spotify" and CMDS == ["next"], (r, CMDS)
print("3. loaded session: next routes to spotify OK")

# 4. a press while another control holds the lock is DROPPED fast (busy)
# instead of queueing behind it
CMDS.clear()
release = threading.Event()
grabbed = threading.Event()


def hog():
    with orch.lock:
        grabbed.set()
        release.wait(10)


t = threading.Thread(target=hog, daemon=True)
t.start()
grabbed.wait(5)
t0 = time.monotonic()
r = orch.command("next")
took = time.monotonic() - t0
release.set()
t.join(5)
assert r.get("busy") is True, r
assert took < 3, f"busy drop took {took:.1f}s — must not queue"
assert CMDS == [], CMDS
print(f"4. press during a running control: dropped in {took:.1f}s OK")

print("SPOTIFY CONTROLS OK — no queue pile-ups, busy is never dead, and "
      "a skip can never restart the album.")
