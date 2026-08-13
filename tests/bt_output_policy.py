#!/usr/bin/env python3
"""Regression gate for btwatchd's follow-the-speaker OUTPUT policy — the
protections that stopped the field 'jumps between episodes like crazy'
flap loop (commits ebf8db2, 9e27dde, ac6ac9b). Pure stubs: no bus, no
hardware, runs anywhere.

The failure it guards against: switching output to bt while the A2DP
transport is still (re)negotiating restarted go-librespot / retargeted
mpv into a dead bluealsa PCM, which errored the track and advanced to
the next episode, over and over — a signalling storm on the shared
radio and a wrecked bookmark.

Rules asserted here:
  1. bt is announced only once the A2DP PCM exists (never mid-setup)
  2. an absent speaker NEVER flips the output to the built-in one
  3. a failed attempt while already connected is a no-op (no churn)
  4. a quick drop->reconnect flap never touches the output
"""

import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install_stubs():
    """Minimal dbus / gi.repository so btwatchd imports with no bus."""
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
            err = type("E", (), {"get_dbus_name":
                                 lambda s: "org.bluez.Error.Failed"})()
            error_handler(err)

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

    timers = []
    delays = []
    glib = types.ModuleType("GLib")
    glib.timeout_add = lambda ms, cb: (timers.append(cb),
                                       delays.append(ms), len(timers))[2]
    glib.source_remove = lambda i: None
    gio = types.ModuleType("Gio")
    gi = types.ModuleType("gi")
    gi.repository = types.ModuleType("gi.repository")
    gi.repository.GLib, gi.repository.Gio = glib, gio
    sys.modules.update({"gi": gi, "gi.repository": gi.repository})
    return connected, timers, delays


def main():
    tmp = tempfile.mkdtemp()
    os.environ.update(
        VIBB_BT_FILE=os.path.join(tmp, "mac"),
        VIBB_BT_LOCKFILE=os.path.join(tmp, "lock"),
        VIBB_RECON_DROP_RETRY="1")
    open(os.environ["VIBB_BT_FILE"], "w").write("AA:BB:CC:DD:EE:FF\n")

    connected, timers, delays = _install_stubs()
    sys.path.insert(0, os.path.join(REPO, "pi"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "btwatchd", os.path.join(REPO, "pi", "btwatchd.py"))
    bw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bw)

    posts = []
    bw.boxapi.post = lambda path, body, timeout=5: (
        posts.append((path, body)), {})[1]

    def outputs():
        return [b["device"] for p, b in posts if p == "/output"]

    def losts():
        return [p for p, _b in posts if p == "/bt/lost"]
    from vibb import btbus
    pcm = {"v": False}
    btbus.a2dp_pcm_present = lambda mac: pcm["v"]

    class StubBus:
        def get_object(self, *a, **k):
            return None

        def add_signal_receiver(self, *a, **k):
            pass

    r = bw.Reconnector(StubBus())

    def fire():
        while timers:
            timers.pop(0)()

    # 1: steady but PCM absent -> no announce; PCM up -> bt announced
    connected["v"] = True
    r.enter_steady("test")
    assert posts == [], f"announced bt with no transport: {posts}"
    pcm["v"] = True
    fire()
    assert outputs() == ["bt"], posts
    print("1. bt announce waits for the A2DP PCM OK")

    # 2: drop -> lost-notify fires ONCE (vibbd stops the player before
    # mpv error-skips the queue) — and the output is NEVER moved to the
    # built-in speaker, however long the speaker stays away (owner
    # decision 2026-08-13). It used to flip after a sustained absence;
    # nothing played through it then, but the next thing to start audio
    # did, at a volume set for quiet headphones. On a bedtime box,
    # silence is the right failure and the speaker is one press away.
    posts.clear()
    connected["v"] = False
    r._props_changed("org.bluez.Device1", {"Connected": False}, [],
                     path=bw.dev_path(r.target))
    assert losts() == ["/bt/lost"], f"drop must notify vibbd: {posts}"
    timers.clear()
    r.state = "WAITING"
    r._attempt_failed("page-timeout")
    assert outputs() == [], f"premature fallback to local: {posts}"
    time.sleep(1.1)
    timers.clear()
    r._attempt_failed("page-timeout")          # long absence: still no
    r._attempt_failed("page-timeout")          # flip, however patient
    assert outputs() == [], \
        f"an absent speaker must never move audio to the box: {posts}"
    print("2. drop notifies vibbd; the box speaker is never taken "
          "automatically OK")

    # 3: a failed attempt while already connected must not churn output
    posts.clear()
    timers.clear()
    connected["v"] = True
    r.state = "STEADY"
    r.announced = "bt"
    r.disconnected_since = None
    r.connecting = r.target
    r._attempt_failed("racing attempt lost")
    assert posts == [] and timers == [], (posts, timers)
    print("3. stale failure while connected is a no-op OK")

    # 4: quick drop->reconnect flap leaves the output alone (the lost
    # notify still fires — vibbd's guard makes it a no-op when nothing
    # was playing into the speaker)
    posts.clear()
    r.announced = "bt"
    connected["v"] = False
    r._props_changed("org.bluez.Device1", {"Connected": False}, [],
                     path=bw.dev_path(r.target))
    connected["v"] = True
    pcm["v"] = True
    r.enter_steady("reconnected")              # back within the window
    fire()
    assert outputs() == [], f"flap churned the output: {posts}"
    assert r.disconnected_since is None
    print("4. quick flap leaves output alone OK")

    # 5: the user switches output to bt while disconnected -> the kick
    # file handler attempts a connect NOW, not after the backoff ladder
    connected["v"] = False
    r.state = "WAITING"
    r.connecting = None
    r.backoff = bw.BACKOFF_MAX_S              # deep in the ladder
    r.last_attempt = 0.0                      # debounce must not block it
    before = r.last_attempt
    r._kicked()
    assert r.last_attempt > before, "kick did not trigger a connect attempt"
    # ...and while already connected, the kick is a harmless no-op
    r._finish_attempt()
    connected["v"] = True
    r.state = "STEADY"
    r.last_attempt = 0.0
    r._kicked()
    assert r.state == "STEADY" and r.connecting is None
    print("5. output-to-bt kick connects immediately, no-op when connected OK")

    # 6: connected but the A2DP transport never appears (speaker's own
    # reconnect brought only AVRCP): exactly ONE profile nudge, a fresh
    # PCM wait after it, then the announce as last resort — never a
    # nudge loop
    posts.clear()
    timers.clear()
    connects = []

    class NudgeIface:
        def __init__(self, *a):
            pass

        def Get(self, i, p, timeout=None):
            return connected["v"]

        def Connect(self, reply_handler=None, error_handler=None,
                    timeout=None):
            connects.append(1)
            reply_handler()  # the nudge succeeds (profiles connect)

    bw.dbus.Interface = lambda o, i: NudgeIface()
    connected["v"] = True
    pcm["v"] = False
    r.state = "WAITING"
    r.announced = None
    r.connecting = None
    r.enter_steady("nudge test")
    fire()  # burns PCM waits -> nudge -> fresh waits -> announce fallback
    assert len(connects) == 1, f"nudge must fire exactly ONCE: {connects}"
    assert [b["device"] for p, b in posts if p == "/output"] == ["bt"], posts
    print("6. missing A2DP gets one profile nudge, no loop OK")

    # ...and when the PCM shows up thanks to the nudge, no fallback spam
    posts.clear()
    timers.clear()
    connects.clear()
    r._nudged = False
    r.state = "WAITING"
    r.announced = None

    class NudgePcmIface(NudgeIface):
        def Connect(self, reply_handler=None, error_handler=None,
                    timeout=None):
            connects.append(1)
            pcm["v"] = True  # the nudge brings the transport up
            reply_handler()

    bw.dbus.Interface = lambda o, i: NudgePcmIface()
    r.enter_steady("nudge test 2")
    fire()
    assert len(connects) == 1 and \
        [b["device"] for p, b in posts if p == "/output"] == ["bt"], \
        (connects, posts)
    print("7. nudge brings the transport up -> clean bt announce OK")

    # 8: a FRESH drop keeps the page cadence tight (a powered-on speaker
    # that doesn't page us is invisible to events — our pages are the
    # only discovery during the blip window); an old drop decays on the
    # normal ladder
    delays.clear()
    connected["v"] = False
    r.state = "WAITING"
    r.connecting = r.target
    r.backoff = 40.0                                   # ladder has grown
    r.disconnected_since = time.monotonic() - 5        # fresh drop
    r._attempt_failed("page-timeout")
    assert delays[-1] == int(bw.RECENT_RETRY_S * 1000), delays
    r.state = "WAITING"
    r.connecting = r.target
    r.backoff = 40.0
    r.disconnected_since = time.monotonic() - bw.RECENT_DROP_S - 1
    r._attempt_failed("page-timeout")
    assert delays[-1] == 40000, delays
    print("8. fresh drop pages every ~15s; old drop decays as before OK")

    print("BT OUTPUT POLICY OK — flap-loop protections intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
