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
  2. local fallback needs the target away for a sustained window
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
    glib = types.ModuleType("GLib")
    glib.timeout_add = lambda ms, cb: (timers.append(cb), len(timers))[1]
    glib.source_remove = lambda i: None
    gio = types.ModuleType("Gio")
    gi = types.ModuleType("gi")
    gi.repository = types.ModuleType("gi.repository")
    gi.repository.GLib, gi.repository.Gio = glib, gio
    sys.modules.update({"gi": gi, "gi.repository": gi.repository})
    return connected, timers


def main():
    tmp = tempfile.mkdtemp()
    os.environ.update(
        TAPBOX_BT_FILE=os.path.join(tmp, "mac"),
        TAPBOX_BT_LOCKFILE=os.path.join(tmp, "lock"),
        TAPBOX_RECON_FALLBACK="1", TAPBOX_RECON_DROP_RETRY="1")
    open(os.environ["TAPBOX_BT_FILE"], "w").write("AA:BB:CC:DD:EE:FF\n")

    connected, timers = _install_stubs()
    sys.path.insert(0, os.path.join(REPO, "pi"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "btwatchd", os.path.join(REPO, "pi", "btwatchd.py"))
    bw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bw)

    posts = []
    bw.boxapi.post = lambda path, body, timeout=5: (posts.append(body), {})[1]
    from tapbox import btbus
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
    assert [p["device"] for p in posts] == ["bt"], posts
    print("1. bt announce waits for the A2DP PCM OK")

    # 2: drop -> within the window no local; sustained absence -> local
    posts.clear()
    connected["v"] = False
    r._props_changed("org.bluez.Device1", {"Connected": False}, [],
                     path=bw.dev_path(r.target))
    timers.clear()
    r.state = "WAITING"
    r._attempt_failed("page-timeout")          # inside FALLBACK window
    assert posts == [], f"premature fallback to local: {posts}"
    time.sleep(1.1)
    timers.clear()
    r._attempt_failed("page-timeout")          # past the window
    assert [p["device"] for p in posts] == ["local"], posts
    print("2. local fallback only after sustained absence OK")

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

    # 4: quick drop->reconnect flap leaves the output alone
    posts.clear()
    r.announced = "bt"
    connected["v"] = False
    r._props_changed("org.bluez.Device1", {"Connected": False}, [],
                     path=bw.dev_path(r.target))
    connected["v"] = True
    pcm["v"] = True
    r.enter_steady("reconnected")              # back within the window
    fire()
    assert posts == [], f"flap churned the output: {posts}"
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

    print("BT OUTPUT POLICY OK — flap-loop protections intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
