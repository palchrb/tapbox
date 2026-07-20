#!/usr/bin/env python3
"""Gate the blind-page parking: a speaker that has been away for
ABSENT_AFTER_S stops costing a ~5s page-TX every 5 minutes on the
shared 2.4GHz radio — the ladder parks instead of flooring at
BACKOFF_MAX forever. Every revival stays event-driven and instant:
the play-press kick and bus evidence reset the ladder and attempt at
once (a powered-on speaker also simply pages US inbound)."""
import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["TAPBOX_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")
os.environ["TAPBOX_BT_KICK"] = os.path.join(TMP, "bt-kick")
sys.path.insert(0, os.path.join(REPO, "pi"))

# stub dbus/gi exactly like bt_radio_yield does (test contract)
dbus_mod = types.ModuleType("dbus")
dbus_mod.Interface = lambda *a, **k: None
dbus_mod.Boolean = bool
mainloop = types.ModuleType("dbus.mainloop")
glib = types.ModuleType("dbus.mainloop.glib")
glib.DBusGMainLoop = lambda **k: None
mainloop.glib = glib
dbus_mod.mainloop = mainloop
gi = types.ModuleType("gi")
repo = types.ModuleType("gi.repository")


class _GLib:
    @staticmethod
    def timeout_add(ms, cb, *a):
        return 1

    @staticmethod
    def source_remove(_id):
        pass


repo.Gio, repo.GLib = types.SimpleNamespace(), _GLib
gi.repository = repo
for name, mod in (("dbus", dbus_mod), ("dbus.mainloop", mainloop),
                  ("dbus.mainloop.glib", glib), ("gi", gi),
                  ("gi.repository", repo)):
    sys.modules[name] = mod

import btwatchd  # noqa: E402

rec = btwatchd.Reconnector(bus=None)
rec._adapter_up = lambda: None
rec._connected = lambda: False
rec._output = lambda dev: None
SCHED = []
rec.schedule = lambda secs, why=None: SCHED.append(secs)
rec.state = "WAITING"

# 1. freshly dropped: fast retries (someone may be power-cycling it)
rec.disconnected_since = time.monotonic() - 5
rec._attempt_failed("Timeout")
assert SCHED and SCHED[-1] == btwatchd.RECENT_RETRY_S, SCHED
print("1. fresh drop keeps the fast retry cadence OK")

# 2. long gone but under the absent threshold: patient ladder still runs
SCHED.clear()
rec.disconnected_since = time.monotonic() - btwatchd.ABSENT_AFTER_S + 60
rec._attempt_failed("Timeout")
assert SCHED, "patient ladder must still schedule below the threshold"
print("2. below the absent threshold the patient ladder still pages OK")

# 3. away past ABSENT_AFTER_S: no more blind pages get scheduled
SCHED.clear()
rec.disconnected_since = time.monotonic() - btwatchd.ABSENT_AFTER_S - 1
rec._attempt_failed("Timeout")
assert SCHED == [], f"absent speaker must park the blind pages: {SCHED}"
print("3. absent speaker parks the blind pages OK")

# 4. the play-press kick revives a parked ladder instantly (backoff
# reset + immediate attempt) — the kid never waits for a timer
rec.target = "AA:BB:CC:DD:EE:FF"
rec.backoff = btwatchd.BACKOFF_MAX_S
ATT = []
rec.attempt = lambda why, **kw: ATT.append(why)
rec._kicked()
assert ATT and rec.backoff == btwatchd.BACKOFF_MIN_S
print("4. play-press kick revives a parked ladder instantly OK")

# 5. bus evidence (the device appearing) revives it too
ATT.clear()
rec.backoff = btwatchd.BACKOFF_MAX_S
rec._ifaces_added(btwatchd.dev_path(rec.target), {})
assert ATT and rec.backoff == btwatchd.BACKOFF_MIN_S
print("5. device-appeared evidence revives a parked ladder OK")

print("BT ABSENT PARK OK — a speaker off for the night stops costing "
      "radio TX, and every wake path stays instant and event-driven.")
