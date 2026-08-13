#!/usr/bin/env python3
"""Gate the on-battery runtime counter's reset logic. It must reset on a
real charge — plugged/charging, or a charge across an OFF period (seen as
a big level rise on the first boot tick) — but NOT on noise: a single
spurious 'plugged' read (debounced over CHARGE_CONFIRM_TICKS) or the
voltage relaxing upward when load drops (guarded by a high rise
threshold). Regressions for the '8h46m runaway' AND the field-reported
'resets for no reason' resets."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("VIBB_STATE", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import sysinfo as s  # noqa: E402

STEP = 60  # one tick's elapsed seconds
# _runtime_step(delta, charging_now, confirmed, rose, prev_accum, pct)

# on battery, level steady -> accumulate
assert s._runtime_step(STEP, False, False, False, 600, 50.0) == (660, 50.0)
print("1. on battery accumulates OK")

# discharge (level fell) -> keep accumulating (pct just persists)
assert s._runtime_step(STEP, False, False, False, 600, 48.0) == (660, 48.0)
print("2. discharge keeps accumulating OK")

# CONFIRMED charge -> reset
assert s._runtime_step(STEP, True, True, False, 600, 50.0) is None
print("3. confirmed charge resets OK")

# charging read but NOT yet confirmed -> HOLD (counter untouched), never
# a reset — one spurious 'plugged' can't wipe the counter
assert s._runtime_step(STEP, True, False, False, 600, 50.0) is s.HOLD
print("4. unconfirmed charge holds, does not reset OK")

# charged while powered OFF: big rise across the off period on the first
# boot tick -> reset
assert s._runtime_step(STEP, False, False, True, 30000, 40.0) is None
print("5. charge-while-off (first-tick rise) resets OK")

# no rise flag mid-session -> accumulate even with a big pct jump (the
# caller only sets rose on the first tick; load-driven noise never does)
assert s._runtime_step(STEP, False, False, False, 30000, 90.0) == (30060, 90.0)
print("6. a mid-session percent jump does NOT falsely reset OK")

# fresh counter (no prior accum) accumulates from zero
assert s._runtime_step(STEP, False, False, False, None, 50.0) == (60, 50.0)
print("7. fresh counter accumulates from zero OK")

# --- the debounce threshold and the relaxation guard, at the raw level

# a single-tick rise BELOW the raised threshold must not qualify as
# 'rose' (this is what the caller computes) — relaxation of ~15% no
# longer trips a reset
assert s.CHARGE_RESET_PCT >= 20, "threshold too low for voltage relaxation"
assert not (55.0 > 42.0 + s.CHARGE_RESET_PCT), \
    "a 13% relaxation rise would still falsely reset"
assert (90.0 > 40.0 + s.CHARGE_RESET_PCT), "a real 50% off-charge must reset"
print("8. threshold clears voltage relaxation but catches real charges OK")

# confirmation needs >= 2 consecutive ticks (a lone blip is tick 1)
assert s.CHARGE_CONFIRM_TICKS >= 2
print("9. charge confirmation needs consecutive ticks OK")

print("BATTERY RUNTIME OK — resets on real charges, not on load noise "
      "or a single flaky read.")
