#!/usr/bin/env python3
"""The dead-output watchdog must not mistake a mashing finger for a
dying sink. Field 2026-08-12: four user nexts inside ten seconds hit
fast_skips>=3 and survive_dead_audio rolled the queue back to the last
audible episode — three times in one minute of browsing. The fix: the
daemon stamps a user-skip marker on every next/prev it routes to mpv,
and the watchdog's fast-skip counter resets while the marker is fresh.
The not-audio_ready() clause is untouched, so a sink that is GENUINELY
gone still triggers on the very next track change."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("TAPBOX_RUN", "TAPBOX_STATE", "TAPBOX_CACHE"):
    os.environ[k] = TMP
os.environ["TAPBOX_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import radio  # noqa: E402
import player  # noqa: E402

# 1. no marker: sub-10s dwells count up — the shipped protection
assert player._count_fast_skip(0, 2.0) == 1
assert player._count_fast_skip(1, 3.0) == 2
assert player._count_fast_skip(2, 1.5) == 3   # would trigger rollback
assert player._count_fast_skip(2, 30.0) == 0  # a long dwell resets
print("1. without a human, fast skips still count to the trigger OK")

# 2. fresh user-skip marker: the counter RESETS — mash is a finger
radio.touch_user_skip()
assert player._count_fast_skip(2, 1.5) == 0
assert player._count_fast_skip(0, 0.5) == 0
print("2. fresh user skip zeroes the count — mash never rolls back OK")

# 3. the marker ages out: protection returns after SKIP_TTL_S
old = time.time() - radio.SKIP_TTL_S - 1
os.utime(radio.SKIP_FILE, (old, old))
assert player._count_fast_skip(2, 1.5) == 3
print("3. stale marker: the dead-output trigger is back OK")

# 4. the daemon stamps the marker on next/prev to a live mpv — and only
#    then (playpause must not suppress the watchdog)
import daemon  # noqa: E402

daemon.go = lambda *a, **k: b"{}"
daemon.go_status = lambda **k: {}
orch = daemon.ORCH
orch._mpv_alive = lambda: True
orch.source = "mpv"                    # a live mpv session owns controls
daemon._kick_bt_connect = lambda *a, **k: None
daemon.mpv_get = lambda prop: {"playlist-pos": 1, "playlist-count": 5,
                               "playback-time": 2}[prop]
daemon.mpv_ipc = lambda cmd: {"error": "success"}
try:
    os.remove(radio.SKIP_FILE)
except OSError:
    pass
orch._command_locked("next")
assert radio.user_skip_fresh(), "next must stamp the user-skip marker"
os.utime(radio.SKIP_FILE, (old, old))
orch._command_locked("playpause")
assert not radio.user_skip_fresh(), "playpause must NOT stamp it"
orch._command_locked("prev")
assert radio.user_skip_fresh(), "prev must stamp the user-skip marker"
print("4. daemon stamps next/prev, never playpause OK")

print("\nMPV MASH ROLLBACK OK — the watchdog tells fingers from "
      "dead sinks.")
