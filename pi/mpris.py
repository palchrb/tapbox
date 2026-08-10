#!/usr/bin/env python3
"""tapbox-mpris — register the box as a BlueZ media player (AVRCP TG).

Why this exists (btsnoop capture 2026-07-27, Skoda/Alps head unit):

1. Car head units POLL the AVRCP player list continuously while
   streaming. With no player registered, BlueZ answers every round with
   errors ('Invalid Player ID' in the capture) — endless control-channel
   chatter DURING live A2DP, which is exactly the channel-ops-while-
   streaming pattern this codebase already knows crashes the BCM43430's
   firmware (see the pairing quiesce in daemon.py). Registering a player
   gives those polls a real answer.
2. With a player registered, the car's display shows what is playing —
   title/artist/album for Spotify, podcasts and uploaded audiobooks
   alike, since everything flows through tapboxd's /status.
3. The car's AVRCP transport commands arrive as PLAYER METHOD calls
   (Play/Pause/Next/...) instead of synthetic key events, and are
   forwarded to the daemon's IDEMPOTENT endpoints — reinforcing the
   937ea05 fix: an explicit "play" can never pause a playing box.

Design notes:
- The registered object speaks the MPRIS Player vocabulary
  (PlaybackStatus / Position / Metadata with xesam:* keys) on the SYSTEM
  bus — the same shape bluez's own mpris-proxy registers, which is what
  org.bluez.Media1.RegisterPlayer parses. No session bus involved.
- State comes from polling GET /status (localhost, SAFE endpoint — no
  token needed). Poll cost is µA-class CPU, no radio.
- PropertiesChanged is emitted only when something MEANINGFUL changed
  (track/status, or position jumping off its extrapolation = a seek).
  BlueZ extrapolates intermediate positions itself from
  PlaybackStatus+Position, so per-tick position spam is pointless and
  would just wake the AVRCP notification machinery.
- bluetooth.service restarts whenever the BT heal runs, so registration
  must survive that: a NameOwnerChanged watch re-registers when bluez
  returns, with retries while the adapter is still coming up.
"""

import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("TAPBOX_DAEMON", "http://127.0.0.1:3679")
ADAPTER = os.environ.get("TAPBOX_BT_ADAPTER", "/org/bluez/hci0")
PLAYER_PATH = "/org/tapbox/player"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
POLL_S = float(os.environ.get("TAPBOX_MPRIS_POLL", "3"))
# a seek is a position that lands off its extrapolation by more than:
SEEK_JUMP_S = 3.0


def log(msg):
    print(f"mpris: {msg}", flush=True)


def get_status(timeout=2):
    with urllib.request.urlopen(BASE + "/status", timeout=timeout) as r:
        return json.loads(r.read())


def post(path):
    """Forward a car command to the daemon. SAFE endpoints, but boxapi
    attaches the box token anyway — and idempotent by design: the car's
    explicit 'play' can never pause a playing box (937ea05)."""
    from tapbox import boxapi
    try:
        boxapi.post(path, {})
    except (OSError, ValueError) as e:
        log(f"{path} failed: {e!r}")


# What each AVRCP/MPRIS method means for the box. Stop maps to pause on
# purpose: a car firing Stop when you turn off the ignition must not
# drop the queue and the bookmark the kid will want on the next ride.
COMMANDS = {
    "Play": "/resume",
    "Pause": "/pause",
    "PlayPause": "/playpause",
    "Stop": "/pause",
    "Next": "/next",
    "Previous": "/prev",
}


def status_to_props(st):
    """tapboxd /status -> MPRIS player properties (what BlueZ parses).
    Position is None when /status carried no position — that means
    UNKNOWN (mpv's IPC socket answers get_property with nothing while
    it's busy), never 0. Callers run the result through carry_position()
    before storing/emitting it."""
    title = st.get("title")
    playing = bool(st.get("playing"))
    status = "Playing" if playing else ("Paused" if title else "Stopped")
    meta = {}
    if title:
        meta["xesam:title"] = str(title)
        sp = st.get("spotify") or {}
        artists = sp.get("artists") or []
        if artists and sp.get("track") == title:
            meta["xesam:artist"] = [str(a) for a in artists]
            if sp.get("album"):
                meta["xesam:album"] = str(sp["album"])
        elif st.get("collection"):
            meta["xesam:artist"] = [str(st["collection"])]
        if st.get("duration"):
            meta["mpris:length"] = int(float(st["duration"]) * 1_000_000)
    pos = st.get("position")
    return {
        "PlaybackStatus": status,
        "Position": None if pos is None else int(float(pos) * 1_000_000),
        "Metadata": meta,
        "CanPlay": True, "CanPause": True, "CanGoNext": True,
        "CanGoPrevious": True, "CanControl": True,
    }


def carry_position(old, new):
    """Fill an UNKNOWN position with the previous one, extrapolated
    while playing. Treating a positionless poll as 0 made every IPC
    hiccup look like a seek-to-start: the car's progress bar slammed to
    0:00 (remaining = full length) and back on alternating polls (field
    2026-07-27, Skoda display).

    mpris:length rides the SAME mpv IPC, so the same hiccup also
    delivers a durationless poll — the Metadata then flapped
    with/without length on alternating ticks, and a length-less track
    renders as a zeroed progress bar on the head unit even with a good
    Position (field 2026-07-30: the display still flapped correct/0
    after the position carry). Carry the old length too, but only while
    the TITLE is unchanged: a real track change must never inherit the
    previous track's duration."""
    out = new
    if new.get("Position") is None:
        base = (old or {}).get("Position") or 0
        if old and old.get("PlaybackStatus") == "Playing" \
                and new.get("PlaybackStatus") == "Playing":
            base += int(POLL_S * 1_000_000)
        out = dict(new)
        out["Position"] = base
    meta = new.get("Metadata") or {}
    old_meta = (old or {}).get("Metadata") or {}
    if meta.get("xesam:title") and "mpris:length" not in meta \
            and old_meta.get("xesam:title") == meta.get("xesam:title") \
            and "mpris:length" in old_meta:
        if out is new:
            out = dict(new)
        out["Metadata"] = dict(meta,
                               **{"mpris:length": old_meta["mpris:length"]})
    return out


def props_changed(old, new):
    """The keys worth signalling: track/status always; position only when
    it jumped off the extrapolation (a seek/track change), because BlueZ
    extrapolates steady playback by itself."""
    new = carry_position(old, new)  # idempotent; direct callers skip it
    if old is None:
        return dict(new)
    out = {}
    for k in ("PlaybackStatus", "Metadata"):
        if new[k] != old[k]:
            out[k] = new[k]
    expect = old["Position"] or 0
    if old["PlaybackStatus"] == "Playing":
        expect += int(POLL_S * 1_000_000)
    if abs(new["Position"] - expect) > SEEK_JUMP_S * 1_000_000 or out:
        if out or abs(new["Position"] - expect) > SEEK_JUMP_S * 1_000_000:
            out["Position"] = new["Position"]
    return out


def main():
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    def to_dbus_meta(meta):
        out = dbus.Dictionary(signature="sv")
        for k, v in meta.items():
            if k == "xesam:artist":
                out[k] = dbus.Array([dbus.String(x) for x in v],
                                    signature="s")
            elif k == "mpris:length":
                out[k] = dbus.Int64(v)
            else:
                out[k] = dbus.String(v)
        return out

    def to_dbus_props(props):
        out = dbus.Dictionary(signature="sv")
        for k, v in props.items():
            if k == "Metadata":
                out[k] = to_dbus_meta(v)
            elif k == "Position":
                out[k] = dbus.Int64(v)
            elif isinstance(v, bool):
                out[k] = dbus.Boolean(v)
            else:
                out[k] = dbus.String(v)
        return out

    class Player(dbus.service.Object):
        def __init__(self):
            super().__init__(bus, PLAYER_PATH)
            self.props = carry_position(None, status_to_props({}))

        # --- org.freedesktop.DBus.Properties ---
        @dbus.service.method("org.freedesktop.DBus.Properties",
                             in_signature="ss", out_signature="v")
        def Get(self, iface, name):
            return to_dbus_props(self.props).get(name)

        @dbus.service.method("org.freedesktop.DBus.Properties",
                             in_signature="s", out_signature="a{sv}")
        def GetAll(self, iface):
            return to_dbus_props(self.props)

        @dbus.service.signal("org.freedesktop.DBus.Properties",
                             signature="sa{sv}as")
        def PropertiesChanged(self, iface, changed, invalidated):
            pass

        # --- org.mpris.MediaPlayer2.Player: the car's buttons ---
        @dbus.service.method(PLAYER_IFACE)
        def Play(self):
            post(COMMANDS["Play"])

        @dbus.service.method(PLAYER_IFACE)
        def Pause(self):
            post(COMMANDS["Pause"])

        @dbus.service.method(PLAYER_IFACE)
        def PlayPause(self):
            post(COMMANDS["PlayPause"])

        @dbus.service.method(PLAYER_IFACE)
        def Stop(self):
            post(COMMANDS["Stop"])

        @dbus.service.method(PLAYER_IFACE)
        def Next(self):
            post(COMMANDS["Next"])

        @dbus.service.method(PLAYER_IFACE)
        def Previous(self):
            post(COMMANDS["Previous"])

    player = Player()
    registered = {"ok": False}

    def register():
        if registered["ok"]:
            return False  # stop the retry timer
        try:
            media = dbus.Interface(bus.get_object("org.bluez", ADAPTER),
                                   "org.bluez.Media1")
            media.RegisterPlayer(dbus.ObjectPath(PLAYER_PATH),
                                 to_dbus_props(player.props))
            registered["ok"] = True
            log(f"registered with bluez on {ADAPTER}")
            return False
        except dbus.DBusException as e:
            # adapter still coming up (mid-heal) — keep trying
            log(f"register failed ({e.get_dbus_name()}) — retrying")
            return True

    def bluez_owner_changed(name, old, new):
        if name != "org.bluez":
            return
        registered["ok"] = False
        if new:
            # bluez (re)started — the BT heal restarts it, so this is
            # the path that keeps the car's display alive across crashes
            log("bluez is back — re-registering")
            GLib.timeout_add_seconds(2, register)
            GLib.timeout_add_seconds(2, refresh_connected)
        else:
            log("bluez went away")
            bt_conn["n"], bt_conn["known"] = 0, True  # no AVRCP consumer

    bus.add_signal_receiver(bluez_owner_changed,
                            signal_name="NameOwnerChanged",
                            dbus_interface="org.freedesktop.DBus",
                            arg0="org.bluez")

    # QA power audit 2026-08-10 #4: the tick polled /status every 3s
    # around the clock — with no BT peer there is nobody to show
    # metadata to or answer AVRCP polls for, yet it was ~1200 of the
    # quiet box's ~1260 thread spawns per hour (every poll is a request
    # thread in tapboxd). Track BlueZ's Device1.Connected and skip the
    # round-trip while nothing is connected. Enumeration failure fails
    # OPEN (keep polling): a few wasted polls beat a silent car display.
    bt_conn = {"n": 0, "known": False}

    def refresh_connected():
        try:
            om = dbus.Interface(bus.get_object("org.bluez", "/"),
                                "org.freedesktop.DBus.ObjectManager")
            bt_conn["n"] = sum(
                1 for ifaces in om.GetManagedObjects().values()
                if bool((ifaces.get("org.bluez.Device1") or {})
                        .get("Connected")))
            bt_conn["known"] = True
        except dbus.DBusException:
            bt_conn["known"] = False  # can't tell — poll as before
        return False  # usable directly as a one-shot GLib timeout

    def device_changed(iface, changed, _invalidated, path=None):
        if iface == "org.bluez.Device1" and "Connected" in changed:
            refresh_connected()  # recount — cheap, and connects are rare

    bus.add_signal_receiver(device_changed,
                            dbus_interface="org.freedesktop.DBus.Properties",
                            signal_name="PropertiesChanged",
                            arg0="org.bluez.Device1",
                            path_keyword="path")
    refresh_connected()

    def tick():
        if bt_conn["known"] and bt_conn["n"] == 0:
            return True  # no BT peer — nobody consumes AVRCP metadata
        try:
            st = get_status()
        except (OSError, ValueError):
            return True  # daemon busy/restarting — keep the last state
        new = carry_position(player.props, status_to_props(st))
        changed = props_changed(player.props, new)
        player.props = new
        if changed and registered["ok"]:
            player.PropertiesChanged(PLAYER_IFACE, to_dbus_props(changed),
                                     [])
        return True

    GLib.timeout_add_seconds(2, register)
    GLib.timeout_add_seconds(int(POLL_S), tick)
    log("up — bridging tapboxd /status to AVRCP")
    GLib.MainLoop().run()


if __name__ == "__main__":
    # repo checkout or installed location for the tapbox package (boxapi)
    _here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(_here, "tapbox")):
        sys.path.insert(0, _here)
    else:
        sys.path.insert(0, "/usr/local/lib/tapbox-py")
    main()
