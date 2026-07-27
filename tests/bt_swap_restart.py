#!/usr/bin/env python3
"""Gate the speaker-swap restart (field 2026-07-27 17:12-17:15).

Switching Skoda -> JBL left the box SILENT on every output: the running
go-librespot resolves tapbox_bt through its process-cached ALSA config,
so after the swap every open tried the departed car's MAC ('PCM not
found' / 'No such device') — including the live reopen_output itself,
which is why no amount of output toggling recovered it.

Rule under test: a MAC CHANGE behind tapbox_bt restarts go-librespot
(fresh process = fresh asound.conf); same-device transitions (crash
heal, blip resume, bt<->local toggles) must NOT restart — the cheap
live reopen is the whole point of v0.0.7."""
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["TAPBOX_BT_FILE"] = os.path.join(TMP, "bt-headset")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

CAR, JBL = "B4:EC:02:4F:36:7C", "2C:FD:B3:5B:1C:BA"
RESTARTS = []
daemon.subprocess = types.SimpleNamespace(
    run=lambda cmd, **k: RESTARTS.append(cmd),
    TimeoutExpired=Exception)
noted = []
daemon._note_go_restart = lambda: noted.append(1)


def set_mac(mac):
    with open(daemon._bt.MAC_FILE, "w") as f:
        f.write(mac + "\n")


# 1. THE BUG: connect lands on a DIFFERENT speaker -> restart, so the
#    next ALSA open resolves the new MAC instead of the ghost
set_mac(JBL)
daemon._go_swap_restart(prev_mac=CAR)
assert RESTARTS and RESTARTS[0][:2] == ["systemctl", "restart"], RESTARTS
assert "go-librespot" in RESTARTS[0], RESTARTS
assert noted == [1], "the restart must be noted (blip-rebuild dedup)"
print("1. speaker swap (car -> JBL) restarts go-librespot OK")

# 2. SAME device reconnecting (heal, blip, plain reconnect): NO restart —
#    the live reopen is cheaper and keeps the session
RESTARTS.clear()
noted.clear()
set_mac(JBL)
daemon._go_swap_restart(prev_mac=JBL)
assert RESTARTS == [] and noted == [], (RESTARTS, noted)
print("2. same-device reconnect does not restart OK")

# 2b. case differences in the MAC must not count as a swap
set_mac(JBL.lower())
daemon._go_swap_restart(prev_mac=JBL)
assert RESTARTS == [], "a case-only difference is the same speaker"
print("2b. MAC case difference is not a swap OK")

# 3. first speaker ever (no previous MAC) restarts too — the process may
#    have resolved tapbox_bt to the null placeholder at startup
set_mac(JBL)
daemon._go_swap_restart(prev_mac="")
assert RESTARTS, "first configured speaker must also refresh the process"
print("3. first-ever speaker restarts (placeholder -> real device) OK")

# 4. connect FAILED (no MAC written / file gone): never restart on that
RESTARTS.clear()
os.remove(daemon._bt.MAC_FILE)
daemon._go_swap_restart(prev_mac=CAR)
assert RESTARTS == [], "a failed connect must not bounce go-librespot"
print("4. failed connect (no configured MAC) does not restart OK")

print("\nall bt_swap_restart checks passed")
