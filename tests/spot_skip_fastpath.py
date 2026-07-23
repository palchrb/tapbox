#!/usr/bin/env python3
"""Gate the spotify next/prev fast-path (go-librespot v0.0.8 integration).

The fork's skip debounce coalesces a press burst into two track loads —
but only if it SEES the burst. The daemon's busy-drop gate serialized
presses ~1s apart (each a fresh leading edge = full load + key request),
defeating the debounce and re-creating the 429 storm (field 2026-07-23:
10 loads in 9s from a prev mash). Contract:

- spotify source + no mpv: EVERY next/prev press is forwarded immediately
  (no busy-drop, no lock held across the HTTP round).
- a failing fast-path (dead session) falls back to the locked path, which
  owns replay-last (next on a dead session still brings music back).
- mpv playback and playpause keep the locked, busy-dropping path."""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
daemon._kick_bt_connect = lambda: None
daemon._radio.touch_busy = lambda: None

sent = []


def slow_command(action):
    sent.append(action)
    time.sleep(0.3)  # a leading-edge load in flight


daemon.spotify_command = daemon.spotify_skip = slow_command
orch.source = "spotify"
orch.target = "https://open.spotify.com/playlist/x"
orch._mpv_alive = lambda: False

# 1. a 5-press burst: EVERY press is forwarded (no busy-drop), calls overlap
for _ in range(5):
    r = orch.command("next")
    assert r.get("fast"), f"spotify next must take the fast path, got {r}"
    time.sleep(0.05)  # 20Hz mash — well inside the old busy window
time.sleep(1.5)
assert len(sent) == 5, f"all 5 presses must reach go-librespot, got {len(sent)}"
print("1. a 5-press burst reaches go-librespot in full (no busy-drop) OK")

# 2. dead session: fast-path fails -> falls back to the locked path (which
#    owns replay-last)
sent.clear()
fallback = []


def dead_command(action):
    sent.append(action)
    raise OSError("session gone")


daemon.spotify_command = daemon.spotify_skip = dead_command
orch._command_locked = lambda action: fallback.append(action)
orch.command("next")
time.sleep(0.5)
assert sent == ["next"] and fallback == ["next"], (sent, fallback)
print("2. dead session falls back to the locked replay path OK")

# 2b. a TIMEOUT is not a dead session: the command usually still lands
# inside go-librespot (mid-settle / 429 backoff). Falling back here
# re-sent the skip and let the locked path read the mid-settle session
# as 'empty' and replay the whole target (field 2026-07-23 22:16:46).
# Contract: stamp the hold window, do NOT fall back.
sent.clear()
fallback.clear()
orch._spot_cmd_timeout_at = -1e9


def slow_to_death(action):
    sent.append(action)
    raise TimeoutError("timed out")


daemon.spotify_command = daemon.spotify_skip = slow_to_death
orch.command("prev")
time.sleep(0.5)
assert sent == ["prev"] and fallback == [], (sent, fallback)
assert orch._spot_cmd_timeout_at > 0, \
    "a fast-path timeout must stamp the hold window (emptiness distrusted)"
print("2b. fast-path timeout: no fallback, no re-send, hold stamped OK")

# 3. mpv playback keeps the locked path (no fast-path for mpv)
orch.source = "mpv"
orch._mpv_alive = lambda: True
locked = []
orch._command_locked = lambda action: locked.append(action) or {"routed": "mpv"}
r = orch.command("next")
assert not r.get("fast") and locked == ["next"], (r, locked)
print("3. mpv keeps the locked path OK")

# 4. playpause keeps the locked path even on spotify
orch.source = "spotify"
orch._mpv_alive = lambda: False
locked.clear()
r = orch.command("playpause")
assert not r.get("fast") and locked == ["playpause"], (r, locked)
print("4. playpause keeps the locked path OK")

print("\nall spot_skip_fastpath checks passed")
