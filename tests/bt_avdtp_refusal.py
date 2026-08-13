#!/usr/bin/env python3
"""Gate the AVDTP-refusal escalation (field 2026-08-04, Skoda + JBL).

The bug: a peer that ACCEPTS the ACL page but never lets the audio
channel up — a car head unit whose single A2DP slot CarPlay holds
(Connection refused), or a headset with a stale session from before a
reboot (Invalid exchange) — looped connect->drop every 3-7s for
minutes. Every ACL success reset the backoff ladder and the away
timer, so neither the 20->300s escalation nor the absence park could
ever engage: the only code that escalates lives in _attempt_failed,
which never runs when the page itself succeeds.

The fix under test: success bookkeeping happens when the A2DP PCM
appears (or the nudge fallback commits), NOT on ACL connect; a drop
before that commit is a refusal that climbs the ladder and eventually
parks; and no side channel (kick, nearby evidence, InterfacesAdded)
resets the ladder while refusals stand — the refusing car is nearby
and chatty by definition, and its own AVRCP transport commands reach
the kick file through the mpris bridge."""
import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(
    VIBB_RUN=TMP, VIBB_STATE=TMP,
    VIBB_BT_FILE=os.path.join(TMP, "mac"),
    VIBB_BT_LOCKFILE=os.path.join(TMP, "lock"),
    VIBB_BT_KICK=os.path.join(TMP, "kick"),
    VIBB_BT_QUIET=os.path.join(TMP, "quiet"))
open(os.environ["VIBB_BT_FILE"], "w").write("B4:EC:02:4F:36:7C\n")


def _install_stubs():
    """Same shim as bt_output_policy.py: no bus, no hardware."""
    dbus = types.ModuleType("dbus")
    connected = {"v": False}

    class StubIface:
        def __init__(self, *a):
            pass

        def Get(self, i, p, timeout=None):
            return connected["v"]

        def Set(self, *a, **k):
            pass

        def Connect(self, reply_handler=None, error_handler=None,
                    timeout=None):
            reply_handler()  # the page succeeds — that IS the field case

    dbus.Interface = lambda o, i: StubIface()
    dbus.Boolean = bool
    dbus.bus = types.ModuleType("dbus.bus")
    dbus.bus.BusConnection = lambda a: None
    dbus.SystemBus = lambda: None
    dbus.mainloop = types.ModuleType("dbus.mainloop")
    dbus.mainloop.glib = types.ModuleType("dbus.mainloop.glib")
    dbus.mainloop.glib.DBusGMainLoop = lambda **k: None
    sys.modules.update({"dbus": dbus, "dbus.bus": dbus.bus,
                        "dbus.mainloop": dbus.mainloop,
                        "dbus.mainloop.glib": dbus.mainloop.glib})
    timers, delays = [], []
    glib = types.ModuleType("GLib")
    glib.timeout_add = lambda ms, cb: (timers.append(cb),
                                       delays.append(ms), len(timers))[2]
    glib.source_remove = lambda i: None
    gi = types.ModuleType("gi")
    gi.repository = types.ModuleType("gi.repository")
    gi.repository.GLib = glib
    gi.repository.Gio = types.ModuleType("Gio")
    sys.modules.update({"gi": gi, "gi.repository": gi.repository})
    return connected, timers, delays


connected, timers, delays = _install_stubs()
sys.path.insert(0, os.path.join(REPO, "pi"))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "btwatchd", os.path.join(REPO, "pi", "btwatchd.py"))
bw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bw)

posts = []
bw.boxapi.post = lambda path, body, timeout=5: (posts.append((path, body)),
                                                {})[1]
from vibb import btbus  # noqa: E402

pcm = {"v": False}
btbus.a2dp_pcm_present = lambda mac: pcm["v"]


class StubBus:
    def get_object(self, *a, **k):
        return None

    def add_signal_receiver(self, *a, **k):
        pass


r = bw.Reconnector(StubBus())
MAC = r.target
PATH = bw.dev_path(MAC)


def drain():
    while timers:
        timers.pop(0)()


def _pcm_ticks():
    """Only the PCM-wait ticks — schedule()'s retry timers stay queued
    (the test drives retry rounds by hand)."""
    return [t for t in list(timers)
            if getattr(t, "__name__", "") == "_await_pcm_tick"]


def drop():
    """The peer/bluez tears the link down; the pending 1s PCM tick then
    fires in WAITING and stands down (in real GLib it always beats the
    3-7s reconnect — an early harness skipped this and left
    _pcm_waiting wedged True across cycles)."""
    connected["v"] = False
    r._props_changed("org.bluez.Device1", {"Connected": False}, [],
                     path=PATH)
    for cb in _pcm_ticks():
        timers.remove(cb)
        cb()


def cycle():
    """One field round: our page succeeds (ACL up), ~2s of PCM polling,
    AVDTP is refused and the link drops."""
    connected["v"] = True
    r.enter_steady("device connected")
    for cb in _pcm_ticks()[:2]:
        timers.remove(cb)
        cb()
    drop()


# 1. ACL-only steady is NOT success: the away stamp and the ladder must
#    survive it (this is the line-340/341 bug — and assert B below is
#    the line-223 half of it)
stamp = time.monotonic() - 100
r.disconnected_since = stamp
r.backoff = 40.0
r.state = "WAITING"
connected["v"] = True
r.enter_steady("device connected")
assert r.disconnected_since == stamp, \
    "an ACL without audio must not clear the away timer"
assert r.backoff == 40.0, "an ACL without audio must not reset the ladder"
print("1. ACL-only steady keeps the away stamp and the ladder OK")

# 2. repeated refusal cycles CLIMB the ladder (fails if only
#    enter_steady is fixed: the drop handler's own reset remains)
delays.clear()
drop()
b1 = r.backoff
cycle()
b2 = r.backoff
assert b2 > b1, f"refusal cycles must escalate: {b1} -> {b2}"
assert r.disconnected_since == stamp, "away time must accrue across cycles"
print(f"2. refusal cycles climb the ladder ({int(b1)}s -> {int(b2)}s) OK")

# 3. after REFUSAL_PARK_N consecutive refusals the pages park: no timer
#    scheduled, and nearby evidence / InterfacesAdded / kicks must not
#    resurrect the 3s loop (the refusing car is right there, chatty,
#    and its own AVRCP commands reach the kick file via mpris)
while r.refusals < bw.REFUSAL_PARK_N:
    cycle()
timers.clear()
delays.clear()
r._props_changed("org.bluez.Device1", {"RSSI": -50}, [], path=PATH)
assert delays == [] and r.backoff > bw.BACKOFF_MIN_S, \
    "nearby evidence must not reset a refusal ladder"
r._ifaces_added(PATH, {})
assert delays == [], "InterfacesAdded must not page a refusal-parked target"
attempts = []
orig_attempt = r.attempt
r.attempt = lambda why, **k: attempts.append(why)
r._kicked()
assert attempts == ["output switched to bt"], \
    "a kick must still fire ONE immediate attempt while parked"
assert r.refusals >= bw.REFUSAL_PARK_N and r.backoff > bw.BACKOFF_MIN_S, \
    "a kick must not clear refusal bookkeeping (the car kicks itself)"
r.attempt = orig_attempt
print("3. refusal park holds against evidence/ifaces/kicks; "
      "kick still attempts once OK")

# 4. inbound connect from the peer revives the PCM check, and REAL
#    audio clears everything — the full success bookkeeping
posts.clear()
pcm["v"] = True
connected["v"] = True
r._props_changed("org.bluez.Device1", {"Connected": True}, [], path=PATH)
drain()
assert [b["device"] for p, b in posts if p == "/output"] == ["bt"], posts
assert r.refusals == 0, "PCM success must zero the refusal count"
assert r.backoff == bw.BACKOFF_MIN_S, "PCM success must reset the ladder"
assert r.disconnected_since is None, "PCM success must clear the away timer"
print("4. inbound connect + real PCM = full success bookkeeping OK")

# 5. blip AFTER real audio keeps the fast path: drop -> DROP_RETRY_S
#    cadence with a fresh ladder (a power-cycled headset must never land
#    in the escalated ladder — RECENT_DROP_S exists for exactly this)
delays.clear()
drop()
assert delays[0] == int(bw.DROP_RETRY_S * 1000), delays
assert r.backoff == bw.BACKOFF_MIN_S, \
    "a drop after real audio is a blip, not a refusal"
assert r.refusals == 0
print("5. drop after real audio keeps the blip fast path OK")

# 6. slow PCM (appears on a later poll) is full success, not a refusal
pcm["v"] = False
connected["v"] = True
r.enter_steady("slow speaker")
for _ in range(3):  # three empty polls...
    if timers:
        timers.pop(0)()
pcm["v"] = True  # ...then the transport lands
drain()
assert r.refusals == 0 and r.disconnected_since is None, \
    "a slow PCM inside the wait window must count as success"
print("6. slow PCM within the wait window is success OK")

# 7. retarget and boot each clear the refusal state (fresh device /
#    fresh bluez deserve a fresh window)
r.refusals = 3
r.backoff = 160.0
open(os.environ["VIBB_BT_FILE"], "w").write("2C:FD:B3:FA:DA:04\n")
r._mac_file_changed()
assert r.refusals == 0, "retarget must clear the refusal count"
r.refusals = 3
r.enter_boot()
assert r.refusals == 0, "a bluez restart must clear the refusal count"
print("7. retarget and boot clear the refusal state OK")

print("\nBT AVDTP REFUSAL OK — a peer that accepts the link but refuses "
      "audio meets the ladder and the park, and a play press still gets "
      "its immediate attempt.")
