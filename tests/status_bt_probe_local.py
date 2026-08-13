#!/usr/bin/env python3
"""Gate: /status only probes the BT transport for the icon when the speaker
is the ACTIVE output.

The bt_connected icon field forked bluealsa-aplay / dbus-enumerated on EVERY
/status (~1/s) whenever a speaker was configured — even while playing on the
built-in speaker, and worst of all it hammered a wedged controller's bluealsa
when the speaker was off/crashed. Now it only probes when output==bt, and
omits the field on local (the UI icon keeps its last value via /system). The
bt_lost/bt_waiting resume path (_bt_wait_state) is unchanged — it already
probes only when a wait intent is pending, so this doesn't starve drop-
recovery."""
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

daemon.go_status = lambda **_k: {}
orch = daemon.ORCH
orch._mpv_alive = lambda: False
orch.target, orch.source = None, None

# a speaker IS configured (so the icon path is in play at all)
daemon._bt.MAC_FILE = os.path.join(os.environ["VIBB_RUN"], "bt-mac")
with open(daemon._bt.MAC_FILE, "w") as f:
    f.write("2C:FD:B3:FA:DA:04")

probes = []
daemon._bt_transport_ready = lambda: (probes.append(1), True)[1]

# 1. output == local (nothing pending): ZERO transport probes, and the
#    bt_connected field is OMITTED (not sent as False)
daemon.current_output = lambda **_k: {"output": "local"}
st = orch.status()
assert probes == [], f"no BT probe on the built-in output, got {probes}"
assert "bt_connected" not in st, \
    "bt_connected must be omitted on local (icon keeps last value via /system)"
print("1. output=local: no BT transport probe, bt_connected omitted OK")

# 2. output == bt: the icon probes and the field is present
probes.clear()
daemon.current_output = lambda **_k: {"output": "bt"}
st = orch.status()
assert st.get("bt_connected") is True, "output=bt must report bt_connected"
assert len(probes) >= 1, "output=bt must probe the transport for the icon"
print("2. output=bt: BT transport probed, bt_connected present OK")

print("\nall status_bt_probe_local checks passed")
