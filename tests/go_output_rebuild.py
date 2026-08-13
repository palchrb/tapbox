#!/usr/bin/env python3
"""Gate the dead-output rebuild (the '2149' bug: go-librespot's ALSA
handle dies WITH the bt transport and stays dead — 'playing' with no
sound). Pre-v0.0.7 the only fix was a restart, which drops the session
and re-bursts the shared radio. v0.0.7's live reopen rebuilds the handle
WITHOUT a restart and keeps the session, so there is nothing to replay
and no login to wait for. Contract: _go_output_rebuild tries the live
reopen first and returns immediately on success; only a pre-v0.0.7 binary
falls through to the restart + wait-for-login path."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

daemon.current_output = lambda: {"output": "bt", "pcm": "vibb_bt"}

RESTARTS = []
daemon.subprocess.run = lambda *a, **k: RESTARTS.append(a)
RETARGETED = []
daemon._retarget_go_librespot = lambda pcm: (RETARGETED.append(pcm) or True)
# login-wait loop breaks as soon as a username is present
daemon.go_status = lambda **k: {"username": "kid"}
daemon._tick = lambda *_a, **_k: None

# 1. v0.0.7 live reopen succeeds -> no restart, no retarget, returns fast
daemon.reopen_go_output = lambda pcm: True
daemon._go_output_rebuild()
assert RESTARTS == [], f"live reopen must not restart: {RESTARTS}"
assert RETARGETED == [], f"live reopen must not rewrite+restart: {RETARGETED}"
print("1. v0.0.7 rebuild: reopened live, no restart, no replay OK")

# 2. pre-v0.0.7 binary (reopen False) with the config already correct and
# NO fresh restart on record -> falls through to the plain restart
daemon.reopen_go_output = lambda pcm: False
daemon._retarget_go_librespot = lambda pcm: False  # config already right
from vibb import paths  # noqa: E402
try:
    os.remove(paths.GO_RESTART_FILE)
except OSError:
    pass
daemon._GO_REBUILD["at"] = 0.0  # not fresh in-process either
RESTARTS.clear()
daemon._go_output_rebuild()
assert len(RESTARTS) == 1, f"old binary must restart to rebuild: {RESTARTS}"
print("2. pre-v0.0.7 rebuild: falls back to restart OK")

# 3. old binary but a restart is already fresh (bt.py just bounced it) ->
# dedup: no second restart
daemon.reopen_go_output = lambda pcm: False
daemon._retarget_go_librespot = lambda pcm: False
paths.note_go_restart()  # a fresh cross-process restart
RESTARTS.clear()
daemon._go_output_rebuild()
assert RESTARTS == [], f"a fresh restart must not be doubled: {RESTARTS}"
print("3. pre-v0.0.7 rebuild: dedups a fresh restart OK")

print("GO OUTPUT REBUILD OK — v0.0.7 reopens the dead handle live with no "
      "restart; an old binary still rebuilds (and dedups) via restart.")
