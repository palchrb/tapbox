#!/usr/bin/env python3
"""Gate the power-cut safety of the two BT state files that do NOT
self-heal. tmp+rename alone is not enough on ext4: the rename can reach
disk before the data, and a hard cut then leaves an EMPTY file — field
2026-08-04: a car-trip cut zeroed /etc/tapbox/bt-headset right after a
follow-the-connector adopt had rewritten it; the box rebooted with
'btwatchd: target (none)', no BT icon, no remembered speaker. An empty
asound.conf is worse still: both pcms gone, every output silent. The
contract: DATA is fsynced before the rename — losing the rename keeps
the old value, never an empty file."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["TAPBOX_ASOUND"] = os.path.join(TMP, "asound.conf")
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import bt  # noqa: E402

bt.log = lambda *a: None
SYNCED = []
_real_fsync = os.fsync


def spy(fd):
    SYNCED.append(1)
    return _real_fsync(fd)


os.fsync = spy

# 1. the configured-speaker file
bt._persist_mac("AA:BB:CC:DD:EE:FF")
assert open(bt.MAC_FILE).read().strip() == "AA:BB:CC:DD:EE:FF"
assert len(SYNCED) == 1, "MAC write must fsync the data before the rename"
print("1. configured-speaker file fsyncs before rename OK")

# 2. asound.conf (the ALSA routing both outputs depend on)
bt._route_alsa("AA:BB:CC:DD:EE:FF")
body = open(bt.ASOUND).read()
assert "AA:BB:CC:DD:EE:FF" in body and "tapbox_local" in body
assert len(SYNCED) == 2, "asound write must fsync the data before the rename"
# already-routed MAC: no rewrite, no extra fsync (SD wear)
bt._route_alsa("AA:BB:CC:DD:EE:FF")
assert len(SYNCED) == 2, "an unchanged route must not rewrite the file"
os.fsync = _real_fsync
print("2. asound.conf fsyncs before rename; unchanged route no-ops OK")

print("\nall bt_state_fsync checks passed")
