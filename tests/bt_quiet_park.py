#!/usr/bin/env python3
"""Gate output-aware reconnect parking: when the USER explicitly chose the
built-in speaker (BT_QUIET_FILE set), btwatchd PARKS its blind reconnect
pages — no timer armed. A speaker DROP never sets the marker (btwatchd's own
fallback=True), so with the marker ABSENT the reconnect ladder still arms:
drop-recovery is preserved."""
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")
os.environ["VIBB_BT_KICK"] = os.path.join(TMP, "bt-kick")
os.environ["VIBB_BT_QUIET"] = os.path.join(TMP, "bt-quiet")
sys.path.insert(0, os.path.join(REPO, "pi"))

# stub dbus/gi like bt_absent_park.py, but RECORD timer arming
armed = []
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
        armed.append(ms)
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

QUIET = btwatchd.BT_QUIET_FILE
rec = btwatchd.Reconnector(bus=None)

# 1. user chose the built-in speaker (marker set) -> schedule PARKS: no timer
open(QUIET, "a").close()
armed.clear()
rec.schedule(5, "timer")
assert armed == [], f"user chose built-in: blind pages must park, got {armed}"
print("1. marker set -> blind reconnect pages parked (no timer) OK")

# 2. marker ABSENT (a speaker DROP is fallback=True, never sets it) -> the
#    reconnect ladder still arms. THIS is the drop-recovery guard.
os.remove(QUIET)
armed.clear()
rec.schedule(5, "target dropped")
assert armed == [5000], f"no marker (a drop): ladder must still fire, got {armed}"
print("2. marker absent -> reconnect ladder still arms (drop-recovery) OK")

print("\nall bt_quiet_park checks passed")
