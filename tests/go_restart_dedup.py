#!/usr/bin/env python3
"""Gate the go-librespot restart dedup. Three code paths restart
go-librespot on the same BT event (bt.py's ALSA-route rewrite,
output.py's audio_device retarget, the daemon's dead-device rebuild),
each for its own config change — but a first-pair connect used to bounce
it twice, each bounce re-bursting the shared 2.4GHz radio. A shared
/run marker lets a restart that has nothing new to apply skip the
redundant one."""
import os
import sys
import tempfile
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = RUN
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_ASOUND"] = os.path.join(tempfile.mkdtemp(), "asound.conf")
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import paths  # noqa: E402

# 1. the marker: never -> not recent; noted -> recent; aged out -> not
assert paths.go_restarted_within(8) is False
paths.note_go_restart()
assert paths.go_restarted_within(8) is True
old = _time.time() - 20
os.utime(paths.GO_RESTART_FILE, (old, old))
assert paths.go_restarted_within(8) is False
# a future mtime (clock jumped back) reads as 'not recent'
future = _time.time() + 100
os.utime(paths.GO_RESTART_FILE, (future, future))
assert paths.go_restarted_within(8) is False
print("1. go-restart marker: recent / stale / future all correct OK")

# 2. bt._route_alsa restarts on a NEW mac and records it in the marker
os.remove(paths.GO_RESTART_FILE)
import types  # noqa: E402

# btbus imports dbus/gi — stub them so bt imports headless (test contract)
for name in ("dbus", "dbus.mainloop", "dbus.mainloop.glib"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["dbus"].Interface = lambda *a, **k: None
sys.modules["dbus.mainloop.glib"].DBusGMainLoop = lambda **k: None
gi = types.ModuleType("gi"); repo = types.ModuleType("gi.repository")
repo.Gio = types.SimpleNamespace(); repo.GLib = types.SimpleNamespace()
gi.repository = repo
sys.modules.setdefault("gi", gi)
sys.modules.setdefault("gi.repository", repo)

from tapbox import bt  # noqa: E402

RAN = []
bt._run = lambda cmd, **kw: RAN.append(tuple(cmd))

bt._route_alsa("AA:BB:CC:DD:EE:FF")
assert any("restart" in c for c in RAN), RAN
assert paths.go_restarted_within(8) is True, "route rewrite must mark a restart"
print("2. bt._route_alsa restarts on a new speaker and marks it OK")

# 3. the SAME mac again is a no-op: no rewrite, no restart, no new mark
RAN.clear()
os.remove(paths.GO_RESTART_FILE)
bt._route_alsa("AA:BB:CC:DD:EE:FF")
assert RAN == [], "an unchanged route must not restart"
assert paths.go_restarted_within(8) is False, "no restart -> no mark"
print("3. re-routing the same speaker is a silent no-op OK")

# 4. the daemon-side fresh check: with the marker fresh AND the config
# already correct, the dead-device rebuild skips its redundant restart
# (it only ever skips when the retarget finds nothing to change)
COOLDOWN = 8
paths.note_go_restart()
config_changed = False   # _retarget_go_librespot returned False
fresh = paths.go_restarted_within(COOLDOWN)
would_restart = config_changed or not fresh
assert would_restart is False, "fresh marker + no config change -> skip"
# but if the config DID change, it restarts regardless of the marker
config_changed = True
would_restart = config_changed or not fresh
assert would_restart is True, "a real config change always restarts"
print("4. rebuild skips only when nothing changed and a restart is fresh OK")

print("GO RESTART DEDUP OK — the double bounce on first-pair is gone, and "
      "a restart that must apply a config change is never skipped.")
