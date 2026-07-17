#!/usr/bin/env python3
"""Event-driven BT reconnect daemon — phase C of PLAN-bt-dbus.md.

Replaces the tapbox-bt-reconnect bash poll loop (up to 60s to notice
the speaker) with BlueZ D-Bus signals: when the remembered speaker
powers on — most page us by themselves, the rest appear on the bus —
we call Device1.Connect within seconds.

State machine (event-driven equivalents of the old loop):

  BOOT       bluez/adapter not confirmed ready: power-on + attempt
             every 5s inside a ~120s window (a failure here means
             "too early", not "speaker away")
  STEADY     target connected: zero timers, pure signal wait
  WAITING    target away: attempt on target-path signals; blind
             backoff attempts 20s -> 300s in between (each blind
             attempt is radio page time — most speakers come to US)
  NO_TARGET  nothing remembered: idle on the MAC-file monitor

Scope guards (plan §7):
- No recovery role: firmware-crash healing lives in bt.py/tapboxd.
  A dead controller just fails our attempts into backoff.
- No pairing, no scanning, no ALSA routing, and no one-output
  enforcement (that stays inside bt.py connect(); auto-kicking a
  self-connecting second device from here would need debounce against
  reconnect loops — deliberately not taken on in phase C).
- Cross-process flock (LOCK_NB) before Connect: when bt.py owns the
  radio (pairing, switching) we skip; a signal or timer retries soon.
- bluez restart (NameOwnerChanged) re-enters the BOOT fast window, so
  recover()'s systemctl restart bluetooth yields a fast reconnect.

Fallback: TAPBOX_BT_BACKEND=cli or missing python3-dbus/python3-gi
exec's the old bash poll loop (installed as tapbox-bt-reconnect-poll).
"""

import os
import subprocess
import sys
import time

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, "/usr/local/lib/tapbox-py"):
    if os.path.isdir(os.path.join(_p, "tapbox")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break

POLL_FALLBACK = os.environ.get("TAPBOX_RECON_POLL",
                               "/usr/local/bin/tapbox-bt-reconnect-poll")


def log(msg):
    print(f"btwatchd: {msg}", flush=True)


def _fallback(reason):
    if os.access(POLL_FALLBACK, os.X_OK):
        log(f"{reason} — falling back to the poll loop")
        os.execv(POLL_FALLBACK, [POLL_FALLBACK])
    log(f"{reason} and no poll fallback installed — exiting")
    sys.exit(3)


if os.environ.get("TAPBOX_BT_BACKEND") == "cli":
    _fallback("TAPBOX_BT_BACKEND=cli")
try:
    import dbus
    import dbus.mainloop.glib
    from gi.repository import Gio, GLib
except ImportError as _e:
    _fallback(f"dbus/gi unavailable ({_e})")

from tapbox import boxapi  # noqa: E402
from tapbox.bt import KICK_FILE, MAC_FILE, acquire_process_lock  # noqa: E402

# timings are env-tunable so the test harness can run in seconds
BOOT_RETRY_S = float(os.environ.get("TAPBOX_RECON_BOOT_RETRY", "5"))
BOOT_WINDOW_S = float(os.environ.get("TAPBOX_RECON_BOOT_WINDOW", "120"))
BACKOFF_MIN_S = float(os.environ.get("TAPBOX_RECON_BACKOFF_MIN", "20"))
BACKOFF_MAX_S = float(os.environ.get("TAPBOX_RECON_BACKOFF_MAX", "300"))
DROP_RETRY_S = float(os.environ.get("TAPBOX_RECON_DROP_RETRY", "3"))
DEBOUNCE_S = float(os.environ.get("TAPBOX_RECON_DEBOUNCE", "5"))
LOCK_RETRY_S = float(os.environ.get("TAPBOX_RECON_LOCK_RETRY", "10"))
FALLBACK_S = float(os.environ.get("TAPBOX_RECON_FALLBACK", "20"))
CONNECT_TIMEOUT_S = 30

BLUEZ = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"


def dev_path(mac):
    return ADAPTER_PATH + "/dev_" + mac.upper().replace(":", "_")


def read_target():
    try:
        mac = open(MAC_FILE).read().strip().upper()
        return mac or None
    except OSError:
        return None


class Reconnector:
    def __init__(self, bus):
        self.bus = bus
        self.target = read_target()
        self.state = "BOOT"
        self.backoff = BACKOFF_MIN_S
        self.timer = None            # at most ONE pending GLib timer
        self.connecting = None       # mac of the in-flight Connect
        self.last_attempt = 0.0
        self.lock = None             # flock held across the Connect
        self.boot_deadline = time.monotonic() + BOOT_WINDOW_S
        self.monitor = None          # Gio ref — GC would stop events
        self.kick_monitor = None     # ditto, for the connect-now kick
        self.announced = None        # last output we told tapboxd about
        self.disconnected_since = None  # when the target went away
        self._pcm_waiting = False    # a bt announcement awaits the PCM
        self._nudged = False         # one A2DP nudge per steady period

    # --- bus plumbing ------------------------------------------------------

    def subscribe(self):
        """No sender filter: only bluez emits these interfaces, and an
        unfiltered match survives bluez restarts with no re-subscribe."""
        add = self.bus.add_signal_receiver
        add(self._props_changed, signal_name="PropertiesChanged",
            dbus_interface="org.freedesktop.DBus.Properties",
            path_keyword="path")
        add(self._ifaces_added, signal_name="InterfacesAdded",
            dbus_interface="org.freedesktop.DBus.ObjectManager")
        add(self._name_owner, signal_name="NameOwnerChanged",
            dbus_interface="org.freedesktop.DBus", arg0=BLUEZ)

    def watch_mac_file(self):
        f = Gio.File.new_for_path(MAC_FILE)
        self.monitor = f.monitor(Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", self._mac_file_changed)

    def watch_kick_file(self):
        f = Gio.File.new_for_path(KICK_FILE)
        self.kick_monitor = f.monitor(Gio.FileMonitorFlags.NONE, None)
        self.kick_monitor.connect("changed", self._kicked)

    # --- signal handlers ---------------------------------------------------

    def _props_changed(self, iface, changed, _invalidated, path=None):
        if str(iface) == "org.bluez.Adapter1":
            if changed.get("Powered"):
                self.attempt("adapter powered")
        elif (str(iface) == "org.bluez.Device1" and self.target
                and str(path) == dev_path(self.target)):
            if "Connected" in changed:
                if changed["Connected"]:
                    self.enter_steady("device connected")
                else:
                    # a blip reconnects fast; a powered-off speaker fails
                    # one page and starts the backoff ladder
                    self.state = "WAITING"
                    self.backoff = BACKOFF_MIN_S
                    self._nudged = False  # fresh nudge for the next link
                    if self.disconnected_since is None:
                        self.disconnected_since = time.monotonic()
                    self._notify_lost()
                    self.schedule(DROP_RETRY_S, "target dropped")
            elif self.state == "WAITING":
                # RSSI etc. — evidence the device is nearby right now
                self.backoff = BACKOFF_MIN_S
                self.attempt("target seen", debounce=True)

    def _ifaces_added(self, path, _ifaces):
        if self.target and str(path) == dev_path(self.target):
            self.backoff = BACKOFF_MIN_S
            self.attempt("target appeared")

    def _name_owner(self, _name, _old, new):
        if str(new):
            log("bluez is up — entering the fast window")
            self.enter_boot()
        else:
            log("bluez went away — going quiet until it returns")
            self.cancel_timer()
            self.state = "BOOT"

    def _kicked(self, *_args):
        """tapboxd touched the kick file: the user just switched the
        output to bt while the speaker is disconnected — connect NOW
        instead of waiting out the backoff ladder. attempt() handles the
        harmless cases (already connected, in flight, lock busy); the
        debounce absorbs Gio's multiple events per touch."""
        if not self.target:
            return
        self.backoff = BACKOFF_MIN_S  # a fresh user intent resets the ladder
        self.attempt("output switched to bt", debounce=True)

    def _mac_file_changed(self, *_args):
        new = read_target()
        if new == self.target:
            return
        log(f"target changed: {self.target or '(none)'} -> {new or '(none)'}")
        self.target = new
        self.backoff = BACKOFF_MIN_S
        self.disconnected_since = None
        self.cancel_timer()
        if new is None:
            self.state = "NO_TARGET"
            self._output("local")  # speaker forgotten -> built-in
        else:
            self.state = "WAITING"
            self.attempt("retarget")

    # --- state transitions ---------------------------------------------------

    def enter_boot(self):
        self.state = "BOOT"
        self.boot_deadline = time.monotonic() + BOOT_WINDOW_S
        self.cancel_timer()
        self._boot_tick()

    def _boot_tick(self):
        if self.state != "BOOT":
            return
        self._adapter_up()
        if not self.connecting:
            self.attempt("boot")
        # next step is scheduled by the attempt's outcome handlers

    def enter_steady(self, why):
        if self.state != "STEADY":
            log(f"steady: {self.target} ({why})")
        self.state = "STEADY"
        self.backoff = BACKOFF_MIN_S
        self.disconnected_since = None
        self.cancel_timer()
        if not self._pcm_waiting:
            self._pcm_waiting = True
            self._pcm_tries = 10
            self._await_pcm()

    def _await_pcm(self):
        """Announce bt only once the A2DP PCM actually exists: the
        announcement restarts go-librespot, and doing that while AVDTP
        is still configuring TORE FRESH CONNECTIONS DOWN (field log:
        SelectCodec 'Resource temporarily unavailable' -> transport
        freed -> disconnect -> fallback to local -> reconnect -> ...,
        an output flap loop that also made mpv skip episodes)."""
        if self.state != "STEADY":
            self._pcm_waiting = False
            return
        try:
            from tapbox import btbus
            ready = btbus.a2dp_pcm_present(self.target)
        except Exception:
            ready = True  # can't tell — announce rather than stall
        if ready:
            self._pcm_waiting = False
            self._nudged = False
            self._output("bt")
            return
        if self._pcm_tries <= 0:
            self._pcm_waiting = False
            if not self._nudged:
                # Connected but no audio transport: some speakers' own
                # reconnect brings only the control link (AVRCP) and the
                # A2DP profile never comes up — the box then sits
                # 'connected but silent' until someone presses connect
                # (field log 2026-07-17 19:02). Device1.Connect on an
                # already-connected device connects the MISSING profiles.
                # Exactly one nudge per steady period, then fall back to
                # today's announce-anyway.
                self._nudged = True
                self._nudge_a2dp()
            else:
                self._output("bt")  # last resort: pre-nudge behavior
            return
        self._pcm_tries -= 1
        GLib.timeout_add(1000, self._await_pcm_tick)

    def _await_pcm_tick(self):
        self._await_pcm()
        return False

    def _nudge_a2dp(self):
        """Force the missing A2DP profile up on an already-connected
        device. Same guards as any attempt: never while another connect
        is in flight, never without the cross-process flock (bt.py may
        own the radio). Success re-enters steady, which re-arms the PCM
        wait; failure announces anyway (the pre-nudge behavior)."""
        if self.state != "STEADY" or self.connecting:
            self._output("bt")
            return
        lock = acquire_process_lock(blocking=False)
        if lock is None:
            self._output("bt")  # bt.py owns the radio — let it finish
            return
        self.connecting = self.target
        self.lock = lock
        log(f"connected but no A2DP transport — nudging profiles "
            f"({self.target})")
        try:
            dev = dbus.Interface(
                self.bus.get_object(BLUEZ, dev_path(self.target),
                                    introspect=False),
                "org.bluez.Device1")
            dev.Connect(reply_handler=self._nudge_ok,
                        error_handler=self._nudge_err,
                        timeout=CONNECT_TIMEOUT_S)
        except Exception:
            self._finish_attempt()
            self._output("bt")

    def _nudge_ok(self):
        self._finish_attempt()
        self.enter_steady("a2dp nudged")  # re-arms the PCM wait

    def _nudge_err(self, err):
        self._finish_attempt()
        try:
            nm = err.get_dbus_name()
        except Exception:
            nm = err.__class__.__name__
        log(f"a2dp nudge failed ({nm}) — announcing anyway")
        self._output("bt")

    def _output(self, device):
        """Follow-the-speaker output policy: connected -> bt, confirmed
        away/forgotten -> built-in (tapboxd skips the fallback when no
        I2S card exists, so BT-only boxes are unaffected). Announced at
        most once per transition — flapping links can't restart
        go-librespot in a loop."""
        self._want_output = device
        if device == self.announced:
            return
        try:
            r = boxapi.post("/output", {"device": device, "fallback": True},
                            timeout=5)
        except Exception as e:
            # tapboxd not up yet (boot: we connect the speaker before the
            # daemon listens) — retry until the announcement lands
            log(f"output -> {device} not applied ({e.__class__.__name__}) "
                f"— retrying in 10s")
            GLib.timeout_add(10000, self._output_retry)
            return
        self.announced = device
        if r.get("skipped"):
            log(f"output -> {device} skipped ({r['skipped']})")
        elif not r.get("unchanged"):
            log(f"output -> {device} (speaker "
                f"{'connected' if device == 'bt' else 'away'})")

    def _output_retry(self):
        want = getattr(self, "_want_output", None)
        if want and want != self.announced:
            self._output(want)
        return False

    def _notify_lost(self):
        """Tell tapboxd the transport just died. mpv reacts to a dead
        ALSA device by ERRORING each episode and auto-advancing — field
        log 2026-07-17: ~15 episodes skipped in 3s before the output
        fallback caught up (the stall watchdog can't see it: the
        position isn't frozen, it's flying). The daemon stops playback
        (bookmark survives) and puts the choice on the screen. A hint,
        not a command: fire-and-forget, no retry — the daemon re-checks
        output + player state itself, and never on the radio path."""
        try:
            boxapi.post("/bt/lost", {}, timeout=3)
        except Exception as e:
            log(f"lost-notify failed ({e.__class__.__name__}) — "
                "the output fallback remains the backstop")

    # --- the attempt ---------------------------------------------------------

    def attempt(self, why, debounce=False):
        if not self.target:
            self.state = "NO_TARGET"
            return
        if self.connecting:
            return
        now = time.monotonic()
        if debounce and now - self.last_attempt < DEBOUNCE_S:
            return
        if self._connected():
            self.enter_steady("already connected")
            return
        lock = acquire_process_lock(blocking=False)
        if lock is None:
            # bt.py owns the radio (pairing/switching) — don't stack a
            # page on top of it; retry shortly
            self.schedule(LOCK_RETRY_S, None)
            return
        self.last_attempt = now
        self.connecting = self.target
        self.lock = lock
        self.cancel_timer()
        log(f"connecting {self.target} ({why})")
        try:
            # fresh proxy per attempt (never cached across bluez restarts);
            # introspect=False keeps proxy creation non-blocking
            dev = dbus.Interface(
                self.bus.get_object(BLUEZ, dev_path(self.target),
                                    introspect=False),
                "org.bluez.Device1")
            dev.Connect(reply_handler=self._connect_ok,
                        error_handler=self._connect_err,
                        timeout=CONNECT_TIMEOUT_S)
        except Exception as e:
            self._attempt_failed(e.__class__.__name__)

    def _connect_ok(self):
        was = self._finish_attempt()
        if was != self.target:
            # retargeted while the old connect was in flight — the
            # steady state we just reached is for the WRONG device
            self.attempt("retarget (stale connect)")
            return
        self.enter_steady("connected")

    def _connect_err(self, err):
        name = getattr(err, "get_dbus_name", lambda: "")() or ""
        if name.endswith(".AlreadyConnected"):
            self._connect_ok()
        else:
            self._attempt_failed(name or str(err))

    def _attempt_failed(self, detail):
        was = self._finish_attempt()
        if was is not None and was != self.target:
            self.attempt("retarget (stale connect)")
            return
        if self.state == "STEADY" or self._connected():
            # a racing attempt lost to a successful connection (the
            # speaker paged us while we paged it) — nothing is wrong,
            # and touching the output here flapped it mid-playback
            return
        if self.state == "BOOT":
            if time.monotonic() < self.boot_deadline:
                self.schedule(BOOT_RETRY_S, None)
                return
            self.state = "WAITING"
        log(f"connect failed ({detail}) — next blind attempt in "
            f"{int(self.backoff)}s")
        if self.disconnected_since is None:
            self.disconnected_since = time.monotonic()
        if time.monotonic() - self.disconnected_since >= FALLBACK_S:
            # away for real — a speaker mid-power-cycle flaps drop/connect
            # for many seconds, and each premature local/bt swing restarts
            # go-librespot and yanks mpv's audio device (episode skips)
            self._output("local")
        self.schedule(self.backoff, None)
        self.backoff = min(self.backoff * 2, BACKOFF_MAX_S)

    def _finish_attempt(self):
        was, self.connecting = self.connecting, None
        lock, self.lock = self.lock, None
        if lock is not None:
            try:
                lock.close()  # flock releases with the fd
            except OSError:
                pass
        return was

    # --- helpers -------------------------------------------------------------

    def _connected(self):
        try:
            props = dbus.Interface(
                self.bus.get_object(BLUEZ, dev_path(self.target),
                                    introspect=False),
                "org.freedesktop.DBus.Properties")
            return bool(props.Get("org.bluez.Device1", "Connected",
                                  timeout=5))
        except Exception:
            return False  # no object / bluez down — attempt will tell

    def _adapter_up(self):
        """BOOT only — mirrors the bash loop's 'power on' retries. Without
        Pairable the eventual pairing would be non-bonding (bt.py lore)."""
        try:
            props = dbus.Interface(self.bus.get_object(BLUEZ, ADAPTER_PATH,
                                                       introspect=False),
                                   "org.freedesktop.DBus.Properties")
            for prop in ("Powered", "Pairable"):
                props.Set("org.bluez.Adapter1", prop, dbus.Boolean(True),
                          timeout=5)
        except Exception:
            pass  # bluez not up yet — that's what the boot window is for

    def schedule(self, secs, why):
        self.cancel_timer()
        if why:
            log(f"{why} — retry in {int(secs)}s")
        self.timer = GLib.timeout_add(int(secs * 1000), self._timer_fire)

    def _timer_fire(self):
        self.timer = None
        if self.state == "BOOT":
            self._boot_tick()
        elif self.state == "WAITING":
            self.attempt("timer")
        return False  # one-shot; outcomes schedule the next one

    def cancel_timer(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None


def main():
    # rfkill runs BEFORE any bus traffic (plan pitfall 10): a blocked
    # radio makes bluez unresponsive, and the block persists reboots
    subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    addr = (os.environ.get("TAPBOX_DBUS_ADDRESS")
            or os.environ.get("DBUS_SYSTEM_BUS_ADDRESS"))
    try:
        bus = dbus.bus.BusConnection(addr) if addr else dbus.SystemBus()
    except Exception as e:
        _fallback(f"cannot reach the system bus ({e.__class__.__name__})")
    bus.set_exit_on_disconnect(True)  # dbus-daemon restart -> systemd respawn
    r = Reconnector(bus)
    r.subscribe()
    r.watch_mac_file()
    r.watch_kick_file()
    log(f"event-driven reconnect up — target {r.target or '(none)'}")
    r.enter_boot()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
