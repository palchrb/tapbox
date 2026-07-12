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
T = s.CHARGE_RESET_PCT

# on battery, level steady -> accumulate
assert s._runtime_step(STEP, "false", "false", 50.0, 600, 50.0) == (660, 50.0)
print("1. on battery accumulates OK")

# normal discharge (level fell) -> keep accumulating
assert s._runtime_step(STEP, "false", "false", 48.0, 600, 50.0) == (660, 48.0)
print("2. discharge keeps accumulating OK")

# plugged in -> reset
assert s._runtime_step(STEP, "true", "false", 50.0, 600, 50.0) is None
print("3. plugged resets OK")

# actively charging while 'plugged' reads false (flaky) -> reset
assert s._runtime_step(STEP, "false", "true", 51.0, 600, 50.0) is None
print("4. charging flag resets even if plugged lies OK")

# charged while powered OFF: level jumped up past the noise floor -> reset
assert s._runtime_step(STEP, "false", "false", 90.0, 30000, 40.0) is None
print("5. a risen level (charge while off) resets OK")

# tiny noise-level rise (< threshold) must NOT reset
assert s._runtime_step(STEP, "false", "false", 50.0 + T - 1, 600, 50.0) \
    == (660, 50.0 + T - 1)
print("6. sub-threshold noise does not falsely reset OK")

# no prior pct (fresh file) -> can't infer a rise, just accumulate
assert s._runtime_step(STEP, "false", "false", 50.0, None, None) == (60, 50.0)
print("7. fresh counter accumulates from zero OK")

print("BATTERY RUNTIME OK — resets on any charge, incl. charge-while-off.")
