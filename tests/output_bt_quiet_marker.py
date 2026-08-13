#!/usr/bin/env python3
"""Gate the BT quiet marker (set_output side): an EXPLICIT user switch to the
built-in speaker (fallback=False) sets it, ANY switch to bt clears it, and
btwatchd's OWN drop-fallback (fallback=True local) never sets it — so
drop-recovery keeps chasing the speaker."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
orch.child = None
orch._mpv_alive = lambda: False
daemon._i2s_card_present = lambda: True         # a built-in card exists
daemon._kick_bt_connect = lambda: None
daemon._bt_transport_ready = lambda: False
daemon.reopen_go_output = lambda pcm: True       # no go-librespot network

QUIET = daemon._bt.BT_QUIET_FILE


def clear():
    try:
        os.remove(QUIET)
    except OSError:
        pass


# 1. user explicitly chose the built-in speaker (fallback=False) -> marker set
clear()
daemon.current_output = lambda **k: {"output": "bt"}    # local IS a change
orch.set_output("local", fallback=False)
assert os.path.exists(QUIET), "user-chosen local must set the quiet marker"
print("1. user hold-X -> local sets the quiet marker OK")

# 2. any switch to bt clears it (so a later drop still recovers)
daemon.current_output = lambda **k: {"output": "local"}  # bt IS a change
orch.set_output("bt", fallback=False)
assert not os.path.exists(QUIET), "a switch to bt must clear the marker"
print("2. switch to bt clears the marker OK")

# 3. btwatchd's OWN drop-fallback (fallback=True local) does NOT set it —
#    the box keeps chasing the speaker it lost
clear()
daemon.current_output = lambda **k: {"output": "bt"}    # local IS a change
orch.set_output("local", fallback=True)
assert not os.path.exists(QUIET), \
    "a drop-fallback local must NOT set the marker (drop-recovery keeps chasing)"
print("3. btwatchd drop-fallback does NOT set the marker OK")

print("\nall output_bt_quiet_marker checks passed")
