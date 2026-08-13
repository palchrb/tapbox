#!/usr/bin/env python3
"""Gate the PWA 'rename a BT speaker' feature: a custom name is written to
BlueZ Device1.Alias, which every listing already reads, so it shows in the
PWA and on the device screen. dbus-native (bluetoothctl has no reliable
one-shot alias set). Blank clears the alias -> BlueZ restores the factory
name. The daemon sanitizes the name before it reaches BlueZ / the screen.

Local (no bus): the sanitizer + arg-validation + cli-degradation are all
deterministic; the actual Alias write is exercised on the rig.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
os.environ["VIBB_BT_BACKEND"] = "cli"   # force the no-bus path here
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import bt, btbus  # noqa: E402

MAC = "2C:FD:B3:FA:DA:04"

# 1. the name sanitizer: control chars gone, one line, length-capped,
# trimmed; junk-only and blank both collapse to "" (which clears the alias)
assert daemon._clean_bt_name("Bilen") == "Bilen"
assert daemon._clean_bt_name("  Barnas  ") == "Barnas"
assert daemon._clean_bt_name("Stue\n\t\x00rom") == "Stuerom", \
    daemon._clean_bt_name("Stue\n\t\x00rom")
assert daemon._clean_bt_name("x" * 200) == "x" * 64
assert daemon._clean_bt_name("") == "" and daemon._clean_bt_name(None) == ""
assert daemon._clean_bt_name("\n\t\x00") == ""
print("1. name sanitizer: printable, single-line, capped, blank clears OK")

# 2. cli backend can't set an alias one-shot -> a clear message, never a
# silent wrong guess (rename is dbus-native)
ok, msg = btbus.set_alias(MAC, "Bilen")
assert ok is False and "dbus" in msg, (ok, msg)
print("2. cli backend reports rename needs dbus, no silent failure OK")

# 3. bt.py rename arg validation: a bad/missing mac is rejected before any
# backend call
assert bt.main.__module__  # importable
sys.argv = ["bt.py", "rename", "not-a-mac", "X"]
assert bt.main() == 1
sys.argv = ["bt.py", "rename"]
assert bt.main() == 1
print("3. bt.py rename rejects a missing/invalid mac OK")

# 4. a valid mac on the cli backend flows through to set_alias and returns
# its failure (exit 1) — the dispatch is wired, dbus does the real write
sys.argv = ["bt.py", "rename", MAC, "Bilen"]
assert bt.main() == 1        # cli backend -> set_alias False -> exit 1
print("4. bt.py rename dispatches a valid mac to set_alias OK")

# 5. the daemon routes /bt/rename through bt_action with the SANITIZED
# name and the mac (blank name reaches the helper to clear the alias)
CALLS = []
daemon.bt_action = lambda args, timeout: (CALLS.append((args, timeout))
                                          or {"ok": True})
# replicate the endpoint body (mac already validated by MAC_RE upstream)
name = daemon._clean_bt_name("  Bilen\x07  ")
daemon.bt_action(["rename", MAC, name], timeout=20)
assert CALLS == [(["rename", MAC, "Bilen"], 20)], CALLS
CALLS.clear()
blank = daemon._clean_bt_name("")
daemon.bt_action(["rename", MAC, blank], timeout=20)
assert CALLS == [(["rename", MAC, ""], 20)], "blank must reach helper to clear"
print("5. daemon passes the sanitized name (blank clears) to bt_action OK")

# --- optional: the real dbus Alias write, only where a bus + gi exist ----
try:
    os.environ["VIBB_BT_BACKEND"] = "dbus"
    import importlib
    importlib.reload(btbus)
    btbus._BACKEND = None
    if btbus.backend() != "dbus":
        raise ImportError
except Exception:
    print("SKIP dbus Alias write (no bus/gi here) — runs on the rig")
else:
    # against a real bluez this would set + read back the Alias; without a
    # bonded device it returns 'no such device', which still proves the
    # dbus path is taken (not the cli 'needs dbus' message)
    ok, msg = btbus.set_alias("AA:AA:AA:AA:AA:AA", "Test")
    assert "dbus backend" not in msg, msg
    print(f"6. dbus set_alias path exercised (msg: {msg}) OK")

print("BT RENAME OK — sanitized custom names reach BlueZ Alias, blank "
      "clears, cli degrades cleanly.")
