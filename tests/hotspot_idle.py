#!/usr/bin/env python3
"""Gate two netmgmt battery fixes. (1) The setup hotspot (AP mode:
beacons 10x/s, wifi power save off, ~40-70mA) stops itself after
HOTSPOT_IDLE_OFF_S with zero associated clients, and the fresh-box
auto-AP does not bring it right back — but boot or an explicit start
re-arms it. (2) Every wifi profile the portal/PWA creates gets NM's
default bgscan stripped (install.sh only covers profiles that existed
at install time; simple:30:-70 is a full off-channel sweep every 30s
at weak signal — A2DP stutter + battery on the shared radio)."""
import os
import sys
import tempfile
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_HOTSPOT_IDLE_OFF"] = "600"
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import netmgmt  # noqa: E402

NMCLI = []


def fake_nmcli(*args, timeout=10):
    NMCLI.append(args)
    return 0, ""


netmgmt._nmcli = fake_nmcli
netmgmt.wifi_state = lambda: (True, "home", "10.0.0.9")
netmgmt._known_wifi_names = lambda: []
netmgmt.wifi_scan = lambda: None
netmgmt.net_changed[0] = lambda: None


def tuned():
    """(bgscan_stripped, ipv6_disabled) across all nmcli calls seen."""
    bg = any("802-11-wireless.bgscan" in a for a in NMCLI)
    v6 = any("ipv6.method" in a and "disabled" in a for a in NMCLI)
    NMCLI.clear()
    return bg, v6


# 1. joining via the portal/PWA tunes the new profile: bgscan off + IPv6
# disabled (the IPv4-only box must not stall on v6 at boot)
netmgmt.wifi_connect("home", "pass1234")
assert tuned() == (True, True), "wifi_connect must strip bgscan AND disable IPv6"
print("1. wifi_connect strips bgscan and disables IPv6 OK")

# 2. pre-provisioning (cabin wifi) tunes it too
netmgmt.wifi_add("cabin", "pass1234")
assert tuned() == (True, True), "wifi_add must strip bgscan AND disable IPv6"
print("2. wifi_add strips bgscan and disables IPv6 OK")


# --- the hotspot idle timeout, driven through the watchdog loop -----------
class StopLoop(Exception):
    pass


STOPPED = []
netmgmt.stop_hotspot = lambda: STOPPED.append(1)
netmgmt.start_hotspot = lambda: STOPPED.append("started")
netmgmt._link_up = lambda: True
netmgmt.hotspot_active = lambda: True
netmgmt.WATCHDOG_DELAY_S = 0


class TimeShim:
    """Scripted sleep scoped to netmgmt's `time` attribute — never the
    shared time module (mutating that while other threads live is the
    Q2 flake mechanism); monotonic etc. stay real."""

    def __init__(self, sleep):
        self.sleep = sleep

    def __getattr__(self, name):
        return getattr(_time, name)


def run_watchdog(ticks):
    left = [ticks]

    def fake_sleep(_s):
        left[0] -= 1
        if left[0] < 0:
            raise StopLoop

    netmgmt.time = TimeShim(fake_sleep)
    try:
        netmgmt._wifi_watchdog()
    except StopLoop:
        pass
    finally:
        netmgmt.time = _time


real_stations = netmgmt._hotspot_stations  # keep the real parser for #7

# 3. a client is connected: the hotspot stays up however long it runs
netmgmt._hotspot_stations = lambda: 1
netmgmt._hs.update(last_client=0.0, idle_stopped=False)
run_watchdog(3)
assert STOPPED == [], "hotspot with a client must never idle-stop"
print("3. hotspot with a connected client stays up OK")

# 4. zero clients past the timeout: stopped, and marked idle_stopped
netmgmt._hotspot_stations = lambda: 0
netmgmt._hs["last_client"] = netmgmt.time.monotonic() - 601
run_watchdog(2)
assert STOPPED and STOPPED[0] == 1, "idle hotspot must stop"
assert netmgmt._hs["idle_stopped"] is True
print("4. hotspot idle past the limit stops itself OK")

# 5. fresh-box auto-AP does NOT resurrect an idle-stopped hotspot
STOPPED.clear()
netmgmt._link_up = lambda: False
netmgmt.hotspot_active = lambda: False
netmgmt.wifi_state = lambda: (True, None, None)
run_watchdog(3)
assert "started" not in STOPPED, "idle-stopped AP must stay down"
print("5. fresh-box auto-AP stays down after an idle-stop OK")

# 6. ...but an explicit start re-arms everything (screen/PWA button)
netmgmt._hs["idle_stopped"] = False  # what start_hotspot() does
run_watchdog(2)
assert "started" in STOPPED, "re-armed fresh box must auto-AP again"
print("6. explicit start re-arms the fresh-box auto-AP OK")

# 7. unreadable station dump fails open: counts as somebody-there, so
# the hotspot can never die mid-setup just because iw hiccupped
def boom(*a, **k):
    raise OSError("no iw")


real_run = netmgmt.subprocess.run
netmgmt.subprocess.run = boom
try:
    assert real_stations() == 1, "unreadable dump must count as a client"
finally:
    netmgmt.subprocess.run = real_run
print("7. station dump unreadable fails open (hotspot stays up) OK")

print("HOTSPOT IDLE OK — no client, no beacons; onboarding survives via "
      "boot/button, and every new wifi profile loses NM's bgscan sweep.")
