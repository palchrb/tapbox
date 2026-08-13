#!/usr/bin/env python3
"""Gate the network-change healer. Field 2026-07-18 23:21: the iPhone
hotspot died, NetworkManager auto-fell back to the home AP, and
go-librespot kept zombie TCPs bound to the OLD address for minutes
(pong/put-state timeouts wedged its api and froze the box) — the old
'network changed' hook only fired on vibbd's own /wifi/connect. The
ip watchdog heals EVERY real address change through one debounced gate."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402


class StopLoop(Exception):
    pass


CALLS = []
daemon.subprocess.run = lambda cmd, **k: CALLS.append(tuple(cmd))


def run_watchdog(ips):
    """Drive the watchdog: ips[0] is the seed, the rest arrive one per
    tick (None = offline)."""
    CALLS.clear()
    seq = list(ips)
    daemon._wlan_ip = lambda: seq.pop(0)
    ticks = [len(ips)]

    def fake_tick(_s):
        if ticks[0] <= 1 or not seq:
            raise StopLoop
        ticks[0] -= 1

    real = daemon._tick
    daemon._tick = fake_tick
    try:
        daemon._ip_watchdog()
    except (StopLoop, IndexError):
        pass
    finally:
        daemon._tick = real
    return [c for c in CALLS if "try-restart" in c]


def cold():  # cooldown gate open
    daemon._GO_REBUILD["at"] = -1e9


# 1. A -> B while the unit runs: exactly one try-restart
cold()
assert len(run_watchdog(["10.0.0.5", "10.0.0.5", "192.168.0.151"])) == 1
print("1. real address change heals with one try-restart OK")

# 2. stable address: zero restarts
cold()
assert run_watchdog(["10.0.0.5"] * 5) == []
print("2. stable address never restarts OK")

# 3. A -> offline -> A: a blip, same lease — sockets still valid
cold()
assert run_watchdog(["10.0.0.5", None, None, "10.0.0.5"]) == []
print("3. offline blip back to the same address: no heal OK")

# 4. A -> offline -> B: heals once
cold()
assert len(run_watchdog(["10.0.0.5", None, "192.168.0.151"])) == 1
print("4. offline then a NEW address heals OK")

# 5. cooldown: a fresh go-librespot restart (unpark/retarget/the other
# trigger) means sockets are already on the new address — skip
daemon._note_go_restart()
assert run_watchdog(["10.0.0.5", "192.168.0.151"]) == []
print("5. recent restart gates the heal (no storm) OK")

# 6. booted offline: the first address seen is the baseline, not a change
cold()
assert run_watchdog([None, "10.0.0.5", "10.0.0.5"]) == []
print("6. first address after offline boot is not a change OK")

# 7. the /wifi/connect hook shares the same gate
daemon._note_go_restart()
daemon._net_changed()
assert not [c for c in CALLS if "try-restart" in c], CALLS
cold()
daemon._net_changed()
assert [c for c in CALLS if "try-restart" in c], "cold hook must restart"
print("7. /wifi/connect hook shares the debounce gate OK")

print("NET HEAL OK — every network change restarts go-librespot once, "
      "blips and fresh restarts never storm it.")
