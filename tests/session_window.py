#!/usr/bin/env python3
"""The power-on session window (owner design 2026-08-13, architect +
QA reviewed). Switch the box off mid-song and come back soon: it
carries on exactly there. Come back days later: it wakes up in the
carousel and a tap starts the album at track 1 again.

The hard part is the clock. The Zero has no RTC, so at boot systemd/
fake-hwclock restore roughly the moment the box was LAST RUNNING —
i.e. approximately when it was switched off — and the PiSugar RTC
correction lands AFTER the daemon starts (field log 2026-08-12: daemon
up 19:58, clock corrected to 20:44). So a naive comparison reads every
session as brand new. Rule: never judge on an untrusted clock, and let
every uncertain case fall through to today's behaviour (continue).

Pins the pure verdict function, the once-per-boot memo, the session's
death at the first tap, and the settings migration."""
import json
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import paths, sysinfo  # noqa: E402

NOW = 1_760_000_000.0
HOUR = 3600.0

# ---- 1. the pure verdict: every uncertain case resolves to 'fresh' ----
import daemon  # noqa: E402

V = daemon.session_age_verdict
# never / always short-circuit before the clock is even consulted
assert V(NOW, 0, NOW, False) == "expired"
assert V(NOW - 999 * HOUR, daemon.SESSION_ALWAYS, NOW, False) == "fresh"
# unstamped (mid-flight upgrade, or a hard cut) must NOT lose the resume
assert V(0, 6, NOW, True) == "fresh"
# an untrusted clock never decides — the caller waits and asks again
assert V(NOW - 99 * HOUR, 6, NOW, False) == "unknown"
# with a trusted clock: inside the window continues, outside starts fresh
assert V(NOW - 2 * HOUR, 6, NOW, True) == "fresh"
assert V(NOW - 6 * HOUR, 6, NOW, True) == "expired"
assert V(NOW - 99 * HOUR, 6, NOW, True) == "expired"
# a stamp from the future = the clock jumped back; no signal, so continue
assert V(NOW + 5 * HOUR, 6, NOW, True) == "fresh"
print("1. verdict: uncertainty always continues, only old sessions expire OK")

# ---- 2. clock trust comes from the RTC marker or NTP ----
assert paths.clock_trusted() is False
paths.note_clock_ok()
assert paths.clock_trusted() is True
os.remove(paths.CLOCK_OK_FILE)
assert paths.clock_trusted() is False
print("2. clock trust marker (tmpfs, per boot) OK")

# ---- 3. session_verdict waits for the clock, then freezes ----
json.dump({"resume_window_h": 6}, open(os.environ["VIBB_SETTINGS"], "w"))
daemon.ORCH.boot_stopped_at = NOW - 99 * HOUR   # switched off days ago
daemon._SESSION.update(verdict=None, live=True)
daemon.BOOT_TICK_S = 0.05
t = threading.Timer(0.3, paths.note_clock_ok)   # RTC lands mid-wait
t.start()
started = time.monotonic()
assert daemon.session_verdict(block_s=5) == "expired"
t.join()
assert 0.2 < time.monotonic() - started < 4, "must resolve when the RTC lands"
assert daemon._SESSION["live"] is False
# frozen: a later call never re-decides (the clock may jump again)
daemon.ORCH.boot_stopped_at = NOW
assert daemon.session_verdict() == "expired"
print("3. verdict waits for the clock, then is frozen for the boot OK")

# ---- 4. a clock that never settles keeps today's behaviour ----
os.remove(paths.CLOCK_OK_FILE)
daemon._SESSION.update(verdict=None, live=True)
daemon.ORCH.boot_stopped_at = NOW - 99 * HOUR
started = time.monotonic()
assert daemon.session_verdict(block_s=0.4) == "fresh"
assert time.monotonic() - started >= 0.4, "must actually wait first"
assert daemon.session_resume() is True
print("4. an unresolvable clock continues the session (no silent loss) OK")

# ---- 5. the session dies at the first tap, not at boot resume ----
assert daemon.session_resume() is True
daemon._SESSION["live"] = False          # what play(boot=False) does
assert daemon.session_resume() is False
assert daemon.session_verdict() == "fresh", "the verdict itself stands"
print("5. first tap ends the session; the verdict is unchanged OK")

# ---- 6. settings: the boolean becomes hours, without demoting anyone ----
S = os.environ["VIBB_SETTINGS"]
json.dump({"resume_on_boot": True}, open(S, "w"))
assert sysinfo.load_settings()["resume_window_h"] == -1, \
    "an upgraded box must keep 'always', not silently become 1 hour"
json.dump({"resume_on_boot": 1}, open(S, "w"))
assert sysinfo.load_settings()["resume_window_h"] == -1
json.dump({"resume_on_boot": False}, open(S, "w"))
assert sysinfo.load_settings()["resume_window_h"] == 0
json.dump({"resume_window_h": 12, "resume_on_boot": True}, open(S, "w"))
assert sysinfo.load_settings()["resume_window_h"] == 12, "explicit wins"
json.dump({}, open(S, "w"))
assert sysinfo.load_settings()["resume_window_h"] == -1, "fresh box: always"
# the retired key is no longer settable, and the range holds
try:
    sysinfo.update_settings({"resume_on_boot": 1})
    raise AssertionError("the retired key must be rejected")
except ValueError:
    pass
for v in (-1, 0, 1, 24, 168):
    assert sysinfo.update_settings({"resume_window_h": v}) is not None
try:
    sysinfo.update_settings({"resume_window_h": 169})
    raise AssertionError("out of range must be rejected")
except ValueError:
    pass
print("6. resume_on_boot -> resume_window_h migration OK")

print("\nSESSION WINDOW OK — continue what was interrupted, forget what "
      "was finished days ago.")
