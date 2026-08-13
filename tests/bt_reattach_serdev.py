#!/usr/bin/env python3
"""Gate the BT firmware re-attach path (field 2026-07-22).

The serdev bus registers in sysfs as 'serial', not 'serdev' — the old
hardcoded /sys/bus/serdev/drivers made the unbind/bind re-probe a silent
no-op: recover()'s rfkill cycles ran, the 'Re-probed' line never appeared,
and a hard hci0 wedge survived everything short of a cold power cycle.
This drives _reattach_firmware against a fake sysfs tree shaped like the
real one (/…/serial/drivers/hci_uart_bcm/serial0-0) and asserts the
unbind/bind writes actually land."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")

# fake sysfs: <TMP>/serial/drivers/hci_uart_bcm/{serial0-0,unbind,bind}
DRV = os.path.join(TMP, "serial", "drivers", "hci_uart_bcm")
os.makedirs(os.path.join(DRV, "serial0-0"))
open(os.path.join(DRV, "unbind"), "w").close()
open(os.path.join(DRV, "bind"), "w").close()
os.environ["VIBB_SERDEV_BASES"] = os.path.join(TMP, "serial", "drivers")

sys.path.insert(0, os.path.join(REPO, "pi"))
from vibb import bt  # noqa: E402

bt.time.sleep = lambda s: None
calls = []
bt._run = lambda cmd, timeout=0: (calls.append(cmd), (1, ""))[1]  # no hciuart

# 1. the re-probe finds the serial-bus driver dir and writes unbind+bind
ok = bt._reattach_firmware()
assert ok, "re-attach must succeed against the serial-bus sysfs layout"
with open(os.path.join(DRV, "unbind")) as f:
    assert f.read() == "serial0-0", "unbind must receive the serdev name"
with open(os.path.join(DRV, "bind")) as f:
    assert f.read() == "serial0-0", "bind must receive the serdev name"
print("1. re-attach unbinds+binds serial0-0 via the 'serial' bus path OK")

# 2. no serdev dirs at all -> honest False (the caller escalates)
os.environ["VIBB_SERDEV_BASES"] = os.path.join(TMP, "missing")
import importlib  # noqa: E402
importlib.reload(bt)
bt.time.sleep = lambda s: None
bt._run = lambda cmd, timeout=0: (1, "")
assert bt._reattach_firmware() is False
print("2. no re-attach path -> returns False (caller escalates) OK")

print("\nall bt_reattach_serdev checks passed")
