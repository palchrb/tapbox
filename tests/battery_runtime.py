#!/usr/bin/env python3
"""Gate the on-battery runtime counter's reset logic: it must reset whenever
the box is (or was) charging — including a charge that happened while the box
was powered off, which shows up as the battery level having risen. Regression
for the '8h46m on battery' runaway where only 'plugged==true' reset it."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TAPBOX_STATE", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import sysinfo as s  # noqa: E402

STEP = 60  # one tick's elapsed seconds
BOOT, MID = True, False  # first tick after boot vs mid-session

# on battery, level steady -> accumulate
assert s._runtime_step(STEP, "false", "false", 50.0, 600, 50.0, MID) == (660, 50.0)
print("1. on battery accumulates OK")

# normal discharge (level fell) -> keep accumulating
assert s._runtime_step(STEP, "false", "false", 48.0, 600, 50.0, MID) == (660, 48.0)
print("2. discharge keeps accumulating OK")

# plugged in -> reset (any tick)
assert s._runtime_step(STEP, "true", "false", 50.0, 600, 50.0, MID) is None
print("3. plugged resets OK")

# actively charging while 'plugged' reads false (flaky) -> reset
assert s._runtime_step(STEP, "false", "true", 51.0, 600, 50.0, MID) is None
print("4. charging flag resets even if plugged lies OK")

# charged while powered OFF: big jump across the off period, seen on the
# FIRST tick after boot -> reset
assert s._runtime_step(STEP, "false", "false", 90.0, 30000, 40.0, BOOT) is None
print("5. charge-while-off (first tick) resets OK")

# THE FALSE-POSITIVE GUARD: the same big rise MID-session (load recovering,
# noisy voltage-modelled percent) must NOT reset — only accumulate
assert s._runtime_step(STEP, "false", "false", 90.0, 30000, 40.0, MID) \
    == (30060, 90.0)
print("6. a mid-session percent jump does NOT falsely reset OK")

# no prior pct (fresh file) -> can't infer a rise, just accumulate
assert s._runtime_step(STEP, "false", "false", 50.0, None, None, BOOT) == (60, 50.0)
print("7. fresh counter accumulates from zero OK")

print("BATTERY RUNTIME OK — resets on real charges, not on load-driven noise.")
