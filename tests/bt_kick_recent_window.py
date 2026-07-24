#!/usr/bin/env python3
"""Gate btwatchd's kick re-basing: a kick (output switched to bt, or the
daemon's post-heal kick) stamps disconnected_since = NOW, so the
RECENT_DROP fast window (15s retries for 150s) runs from recovery
completion — a slow controller heal must not burn the window that
started at the original drop and land in the patient 20->300s ladder."""
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
os.environ["TAPBOX_BT_QUIET"] = os.path.join(TMP, "bt-quiet")
sys.path.insert(0, os.path.join(REPO, "pi"))

# stub dbus/gi (same shim as bt_quiet_park.py)
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
rec.target = "2C:FD:B3:FA:DA:04"
attempts = []
rec.attempt = lambda reason, **k: attempts.append(reason)

# 1. a kick stamps disconnected_since = now (re-bases the fast window)
rec.disconnected_since = time.monotonic() - 500  # old drop, window burnt
rec.backoff = 300
rec._kicked()
assert attempts == ["output switched to bt"], attempts
away = time.monotonic() - rec.disconnected_since
assert away < 5, f"kick must re-base disconnected_since to NOW, away={away}"
assert rec.backoff == btwatchd.BACKOFF_MIN_S, "kick resets the ladder"
assert away < btwatchd.RECENT_DROP_S, "re-based => back in the 15s cadence"
print("1. kick re-bases disconnected_since (fresh RECENT window) OK")

# 2. no target: the kick is a no-op (no stamp, no attempt)
attempts.clear()
rec.target = None
rec.disconnected_since = None
rec._kicked()
assert attempts == [] and rec.disconnected_since is None
print("2. kick without a target: no-op OK")

print("\nall bt_kick_recent_window checks passed")
