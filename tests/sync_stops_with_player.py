#!/usr/bin/env python3
"""Gate the per-play sync's lifetime: it must die WITH player.py. Field
2026-07-18: playing a 336-episode RSS feed started its background sync;
switching to Spotify killed player.py but the sync child survived as an
orphan and kept downloading through the Spotify stream — the exact
shared-radio contention the sweep's busy-gate prevents. The SIGTERM
path (stop / target switch / reboot) must terminate the sync child; a
natural queue end leaves it be (idle box — finishing is free)."""
import inspect
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402


class FakeProc:
    def __init__(self, alive=True):
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False


# 1. a running sync child is terminated and the handle cleared
p = FakeProc()
player._sync_child = p
player._stop_sync_child()
assert p.terminated is True
assert player._sync_child is None
print("1. running sync child is terminated on player stop OK")

# 2. an already-finished child is left alone (no bogus terminate)
p = FakeProc(alive=False)
player._sync_child = p
player._stop_sync_child()
assert p.terminated is False
print("2. finished child: no-op OK")

# 3. no child at all: safe no-op
player._sync_child = None
player._stop_sync_child()
print("3. no child: safe no-op OK")

# 4. the SIGTERM handler is actually wired to it, and the spawn stores
# the child in the module global the handler reaches
src = inspect.getsource(player.main)
assert "_stop_sync_child()" in src, "SIGTERM handler must kill the sync"
assert "_sync_child = subprocess.Popen" in src, \
    "the sync spawn must store the child where the handler finds it"
print("4. SIGTERM handler wired to the sync child OK")

print("SYNC LIFETIME OK — the per-play sync dies with its player, never "
      "orphaned under the next source's stream.")
