#!/usr/bin/env python3
"""Gate the cache sweep's yield-to-playback: the sweep's downloads share
the single 2.4GHz radio with the Spotify stream AND the A2DP link, and a
saturated radio starves control calls and fools the offline prober
(field 2026-07-18: pausing a song fought the sweep for two minutes). The
sweep must hold off while anything is audible, and an IN-FLIGHT download
must be abandoned (terminated, retried next sweep) when playback starts."""
import os
import subprocess
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

# stand-in for content.py: a child that sleeps "forever" (or exits fast)
SLEEPER = os.path.join(os.environ["VIBB_STATE"], "fake_content.py")
with open(SLEEPER, "w") as f:
    f.write("import sys, time\n"
            "time.sleep(0.1 if sys.argv[1] == 'fast' else 300)\n")
lib.content.__file__ = SLEEPER
lib._TASKSET = None

# 1. not busy: a sync runs to completion
lib.BUSY_CHECK = lambda: False
t0 = time.monotonic()
lib._sync_one(["fast"])
assert time.monotonic() - t0 < 10
print("1. idle box: a sync runs to completion OK")

# 2. playback starts MID-download -> the child is terminated and
# SweepYield raised within a poll tick or two — the radio belongs to
# the music, not to a 300s download
busy_after = time.monotonic() + 1.0
lib.BUSY_CHECK = lambda: time.monotonic() > busy_after
t0 = time.monotonic()
try:
    lib._sync_one(["slow"])
    raise SystemExit("FAIL: sync ran to completion despite playback")
except lib.SweepYield:
    pass
took = time.monotonic() - t0
assert took < 15, f"yield took {took:.1f}s — must be within a few polls"
# ...and the sleeper is actually dead (no orphaned download)
r = subprocess.run(["pgrep", "-f", "fake_content.py"],
                   capture_output=True, text=True)
assert r.stdout.strip() == "", "download child left running after yield"
print(f"2. playback mid-download -> terminated + SweepYield in {took:.1f}s OK")

# 3. a busy box holds the sweep's entry gate (the _busy() helper)
lib.BUSY_CHECK = lambda: True
assert lib._busy() is True
lib.BUSY_CHECK = lambda: False
assert lib._busy() is False
print("3. busy gate reflects the daemon's audible-check OK")

# 4. a BROKEN busy-check never stalls or crashes the sweep — treated as
# not-busy (the sweep is background work; failing open is correct)
def _boom():
    raise RuntimeError("status exploded")

lib.BUSY_CHECK = _boom
assert lib._busy() is False
lib._sync_one(["fast"])  # still completes
print("4. a broken busy-check fails open (sweep continues) OK")

# 5. no wiring at all (CLI use): never busy, sync runs
lib.BUSY_CHECK = None
assert lib._busy() is False
print("5. unwired (CLI) sweeps behave as before OK")

print("SWEEP YIELD OK — active listening owns the radio; downloads "
      "abandon within seconds and retry next sweep.")
