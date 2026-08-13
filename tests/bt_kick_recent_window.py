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
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")
os.environ["VIBB_BT_KICK"] = os.path.join(TMP, "bt-kick")
os.environ["VIBB_BT_QUIET"] = os.path.join(TMP, "bt-quiet")
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

# 1. a kick from OUTSIDE the fast window (an old drop / a heal after a
#    long absence) re-bases disconnected_since to NOW — the post-heal
#    retry then runs the 15s cadence for a fresh 150s
rec.disconnected_since = time.monotonic() - (btwatchd.RECENT_DROP_S + 350)
rec.backoff = 300
rec._kicked()
assert attempts == ["output switched to bt"], attempts
away = time.monotonic() - rec.disconnected_since
assert away < 5, f"a kick past the window must re-base to NOW, away={away}"
assert rec.backoff == btwatchd.BACKOFF_MIN_S, "kick resets the ladder"
print("1. kick past the window re-bases disconnected_since OK")

# 1b. a kick INSIDE the fast window must NOT re-base — else every button
#     press with the headset off restarts the 150s×15s paging and resets
#     the 1h ABSENT clock, so a kid mashing buttons pages ~4/min forever
#     (energy/RF audit 2026-07-24 #3). The immediate attempt() still runs.
attempts.clear()
stamp = time.monotonic() - 30  # 30s into the window
rec.disconnected_since = stamp
rec._kicked()
assert attempts == ["output switched to bt"], "the immediate attempt must run"
assert rec.disconnected_since == stamp, \
    "a kick inside the window must NOT restart it"
print("1b. kick inside the window keeps the stamp (no endless paging) OK")

# 1c. a kick with NO drop recorded (fresh intent, never disconnected)
#     stamps NOW so the window is defined
attempts.clear()
rec.disconnected_since = None
rec._kicked()
assert rec.disconnected_since is not None and \
    time.monotonic() - rec.disconnected_since < 5
print("1c. kick with no prior drop stamps the window OK")

# 2. no target: the kick is a no-op (no stamp, no attempt)
attempts.clear()
rec.target = None
rec.disconnected_since = None
rec._kicked()
assert attempts == [] and rec.disconnected_since is None
print("2. kick without a target: no-op OK")

print("\nall bt_kick_recent_window checks passed")
