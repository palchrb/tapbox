#!/usr/bin/env python3
"""Gate the cache sweeper's reboot TTL: the sweep must NOT re-run seconds
after every boot when the last one was within SYNC_INTERVAL — that
redundant download+transcode burst stole a track skip on a fresh boot
(field log 2026-07-17). A recent stamp defers the first sweep to the
remainder of the interval; a stale/absent stamp sweeps at SYNC_DELAY."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import library as lib  # noqa: E402


def due_in(now):
    """Reproduce the sweeper's first-wait computation."""
    return max(lib.SYNC_DELAY_S,
               lib._last_sweep() + lib.SYNC_INTERVAL_S - now)


# 1. no stamp yet (fresh box) -> first sweep at SYNC_DELAY
assert lib._last_sweep() == 0.0
assert due_in(time.time()) == lib.SYNC_DELAY_S
print("1. fresh box sweeps at SYNC_DELAY OK")

# 2. a sweep just completed -> next one deferred ~a full interval, not
# re-run at SYNC_DELAY on the next boot
lib._stamp_sweep()
d = due_in(time.time())
assert d > lib.SYNC_INTERVAL_S - 60, f"recent sweep not deferred: {d}"
print("2. a recent sweep defers the next by ~the interval OK")

# 3. an old stamp (older than the interval) -> sweep promptly at SYNC_DELAY
import json  # noqa: E402
with open(lib.SWEEP_STAMP, "w") as f:
    json.dump({"at": time.time() - lib.SYNC_INTERVAL_S - 3600}, f)
assert due_in(time.time()) == lib.SYNC_DELAY_S, "stale stamp must not defer"
print("3. a stale stamp sweeps promptly OK")

# 4. a corrupt stamp is treated as absent (never crash the sweeper)
with open(lib.SWEEP_STAMP, "w") as f:
    f.write("{ not json")
assert lib._last_sweep() == 0.0
print("4. corrupt stamp treated as fresh, no crash OK")

print("SWEEP TTL OK — no redundant sweep on every reboot.")
