#!/usr/bin/env python3
"""tapboxd — TapBox orchestration daemon: one authority for playback.

Owns the answer to "what is playing / what played last" and routes all
commands, so cards, buttons, the CLI and (later) the parent PWA behave
coherently instead of guessing at each other. HTTP API on 127.0.0.1:3679:

  POST /play       {"target": <any link/path>, "fresh": bool,
                    "episode": <id>}  episode = start the queue there
  POST /playpause  |  /pause  |  /next  |  /prev  |  /stop
  POST /shuffle    {"enabled": bool} — mpv reshuffles the playlist,
                   Spotify toggles shuffle_context
  POST /volume     {"volume": 0-100} or {"delta": +/-n} — routes to the
                   active source (mpv softvol / go-librespot volume)
  GET  /volume     current volume of the active source (0-100)
  GET  /status     unified now-playing (source, title, position, ...)
  GET  /library    the parent-curated library (sections -> named links)
  PUT  /library    replace the library (validated, atomic write)
  POST /library/section-logo  {"id": <section>, "data": <base64|null>}
                   upload/remove a home-screen logo for a category
  GET  /expand?id=<entry>|target=<url>   entry -> playable episode list
                   with titles + cached flags (offline-aware menus)
  GET  /output     current audio output ("bt" or "local")
  POST /output     {"device": "bt"|"local", "fallback": bool} — mpv
                   switches live over IPC; fallback=true (btwatchd's
                   follow-the-speaker policy) is skipped without an I2S card;
                   go-librespot needs a config rewrite + service restart
  GET  /settings   box settings (screen timeout, idle shutdown, volume cap)
  PUT  /settings   update settings (validated; consumers re-read live)
  GET  /system     battery (PiSugar), disk/cache usage, wifi state, temps
  POST /system/wifi      {"enabled": bool} — rfkill wifi
  POST /system/shutdown  {"restart": bool} — graceful poweroff/reboot
  POST /wifi/scan     list nearby networks (ssid/signal/secured/known)
  POST /wifi/connect  {"ssid", "password"?} — join a network (nmcli);
                      leaves the setup hotspot first, restores it on failure
  POST /wifi/forget   {"ssid"} — delete the saved profile
  POST /wifi/add      {"ssid", "password"?} — save a profile WITHOUT the
                      network in range (pre-provision the cabin wifi);
                      auto-joins when first seen
  POST /spotify/logout   forget the Spotify login (drop credentials +
                         restart go-librespot) — the new account then picks
                         the box under Devices in the Spotify app
  POST /wifi/hotspot  {"enabled": bool} — the setup hotspot (TapBox-<host>).
                      Also auto-starts on fresh boxes: no saved wifi network
                      and nothing connected. A :80 redirect server + wildcard
                      DNS (dnsmasq-shared.d) pops the phone's captive portal
                      straight into the PWA.
  GET  /bt         known/paired/connected speakers + the configured one
  POST /bt/scan    scan ~20s, list nearby devices (pick one -> /bt/connect)
  POST /bt/pair    {"name"?} — one-button flow: auto-pair the single audio
                   device in pairing mode (play.sh's validated flow)
  POST /bt/lost    internal (btwatchd): the speaker's transport died —
                   stop mpv before it error-skips the queue, arm the
                   screen's "disconnected" choice popup
  POST /bt/visible {"secs"?} — incoming pairing mode: the box becomes
                   discoverable for ~2 min and accepts a pairing started
                   FROM a car/head unit; the new bond shows up in GET /bt
                   for the parent to pick as speaker (never auto-adopted)
  POST /bt/connect {"mac"}  — connect a speaker; pairs first when the mac
                   is new (picked from a scan), routes audio to it
  POST /bt/forget  {"mac"}  — drop the bond (permanent)
  POST /bt/disconnect {"mac"} — hang up without forgetting

The library lives in /etc/tapbox/library.json ON THE BOX — menus must
render (and cached content must play) with no internet at all. A future
parent cloud service is a sync mirror of this file, never the source.

Command routing:
  1. mpv session running (player.py child)  -> mpv IPC
  2. Spotify actively playing (also when started from the phone) -> go-librespot
  3. last source was Spotify                -> go-librespot
  4. otherwise, remembered target           -> re-play it (bookmark resumes)

Rule 4 is the fix for "short press after a stopped podcast wakes some
old Spotify track": a dead session's controls bring back what YOU last
played, at the position you left it.

Playback itself is delegated: /play spawns player.py, which routes
Spotify links to go-librespot and everything else to mpv-with-resume.
The daemon stays a thin, state-owning router.
"""

import base64
import hashlib
import json
import mimetypes
import os
import signal
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The tapbox package sits next to this script in the repo, or under
# /usr/local/lib/tapbox-py when installed. Repo wins; exactly one is used.
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, "/usr/local/lib/tapbox-py"):
    if os.path.isdir(os.path.join(_p, "tapbox")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from tapbox import content, mpv as _mpv, spotify as _spotify  # noqa: E402
from tapbox import spotify_web as _spotify_web  # noqa: E402
from tapbox.paths import ART_DIR, STATE_DIR  # noqa: E402

# Module-level aliases: internal code (and the tests, which monkeypatch
# these names) keeps calling daemon.<helper>.
is_spotify = _spotify.is_spotify
go = _spotify.go
go_status = _spotify.status
spotify_playing = _spotify.playing
spotify_command = _spotify.command
mpv_ipc = _mpv.ipc
mpv_get = _mpv.get

LAST_FILE = os.path.join(STATE_DIR, "last-play.json")
VOL_FILE = os.path.join(STATE_DIR, "volume.json")
NOW_FILE = os.path.join(STATE_DIR, "now-playing.json")
QUEUE_FILE = os.path.join(STATE_DIR, "now-queue.json")

_QUEUE_CACHE = {"mtime": None, "data": None}

# poked on spotify plays: the bookmarker idles at a 30s heartbeat between
# sessions, which let a short play (<30s) end entirely between ticks —
# no bookmark ever written ("no spotify bookmark on disk" later)
_bm_wake = threading.Event()

# the supervisor's (and play-path's) verdict on actual internet — surfaced
# in /status as spotify_offline so the clients can SAY "no internet"
# instead of silently failing (wifi can be up while the WAN is dead)
_SPOT_OFFLINE = [False]


def _queue_map():
    """player.py's url -> {id,title,image} map for the running queue,
    parsed once per spawn (mtime-cached — /status polls every second)."""
    try:
        m = os.path.getmtime(QUEUE_FILE)
    except OSError:
        return None
    if _QUEUE_CACHE["mtime"] != m:
        try:
            with open(QUEUE_FILE) as f:
                _QUEUE_CACHE["data"] = json.load(f)
            _QUEUE_CACHE["mtime"] = m
        except (OSError, ValueError):
            return None
    return _QUEUE_CACHE["data"]
PORT = int(os.environ.get("TAPBOX_PORT", "3679"))
PORTAL_PORT = int(os.environ.get("TAPBOX_PORTAL_PORT", "80"))
# The parent PWA is served to the LAN (http://tapbox.local:3679). Keep this
# port firewalled from the internet — the API is deliberately auth-less on
# the home network (a PIN gate is a product-phase addition).
BIND = os.environ.get("TAPBOX_BIND", "0.0.0.0")
# restart playback when it claims to play but makes no progress this long
STALL_S = float(os.environ.get("TAPBOX_STALL_S", "30"))
# how often the stall watchdog samples position + radio TX counters
STALL_POLL_S = float(os.environ.get("TAPBOX_STALL_POLL", "5"))
WEB_DIR = os.environ.get("TAPBOX_WEB") or (
    os.path.join(_here, "web") if os.path.isdir(os.path.join(_here, "web"))
    else "/usr/share/tapbox/web")


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def player_path():
    p = os.path.join(_here, "player.py")
    return p if os.path.exists(p) else "/usr/local/bin/tapbox-player"


# --- moved to the tapbox package; aliases keep internal call sites and the
# --- tests' daemon.<name> monkeypatching working unchanged ----------------------

from tapbox import bt as _bt, btbus, netmgmt as _netmgmt  # noqa: E402
from tapbox.library import (  # noqa: E402
    ORDERS, artwork_allowed, expand_target, find_entry, library_with_covers,
    load_library, normalize_library, save_library, state_key, _cache_sweeper,
    _natural_order, _sync_wake)
from tapbox.netmgmt import (  # noqa: E402
    HOTSPOT_PSK, HOTSPOT_SSID, hotspot_active, set_wifi, start_hotspot,
    stop_hotspot, wifi_add, wifi_connect, wifi_forget, wifi_scan,
    wifi_state, _wifi_watchdog)
from tapbox.output import (  # noqa: E402
    OUTPUT_PCMS, OUT_FILE, audio_ready, current_output, _i2s_card_present,
    _retarget_go_librespot)
from tapbox.sysinfo import (  # noqa: E402
    load_settings, shutdown, system_status, update_settings,
    _battery_runtime_tracker)

MAC_RE = _bt.MAC_RE
bt_status = _bt.bt_status
bt_action = _bt.bt_action
bt_scan = _bt.bt_scan


# --- the orchestrator ----------------------------------------------------------

class Orchestrator:
    def __init__(self):
        self.lock = threading.Lock()
        self.child = None
        self.target = None
        self.source = None
        self.reverse = False
        self.resume = True  # library 'from start' entries set this False
        self.mpv_shuffle = False  # mpv has no queryable shuffle state
        self.spot_pending = None  # a freshly tapped spotify target is
        # loading: go-librespot still describes the PREVIOUS context —
        # /status shows the tapped entry's own identity meanwhile
        try:
            with open(LAST_FILE) as f:
                d = json.load(f)
            self.target, self.source = d.get("target"), d.get("source")
            self.reverse = bool(d.get("reverse"))
            self.resume = bool(d.get("resume", True))
            if self.target:
                log(f"remembered last play: [{self.source}] {self.target}")
        except (OSError, ValueError):
            pass
        self.child_started = 0.0
        threading.Thread(target=self._arbiter, daemon=True).start()
        threading.Thread(target=self._stall_watchdog, daemon=True).start()

    def _arbiter(self):
        """The box stays Spotify Connect-discoverable while mpv plays; if the
        user picks it from the phone mid-podcast, both would fight over the
        BT output. Watch for that takeover and yield mpv gracefully (its
        bookmark is saved, so the card resumes later)."""
        while True:
            time.sleep(4)
            try:
                with self.lock:
                    alive = self._mpv_alive()
                    age = time.monotonic() - self.child_started
                # grace period: player.py pauses spotify right after starting,
                # don't mistake that brief overlap for a takeover
                if not alive or age < 10:
                    continue
                if spotify_playing():
                    with self.lock:
                        if self._mpv_alive():
                            log("spotify took over (phone) — yielding mpv")
                            self._stop_child()
                            self.source = "spotify"
                            self._persist()
            except Exception as e:  # a dead arbiter = silent feature loss
                log(f"arbiter error: {e!r}")

    def _stall_watchdog(self):
        """A dropped BT speaker can wedge mpv: the process stays alive but
        audio writes block, the position freezes, and every button press
        routes into a wall — the box looks hung until someone reboots it.
        Watch for 'claims to be playing but no progress for STALL_S', then
        restart playback (the 3s bookmark resumes it in place) once the
        output is able to make sound again.

        A second failure mode leaves the position TICKING: bluez still
        says connected, bluealsa still lists the PCM, mpv keeps decoding —
        but nothing leaves the radio (a zombie transport). The controller's
        TX byte counter is ground truth there: A2DP moves ~35kB/s, so a
        counter that stays flat across STALL_S of claimed playback means
        the link is dead and must be torn down and rebuilt — waiting on
        _audio_ready() would never fire, since bluez keeps lying."""
        last_pos, last_change = None, time.monotonic()
        last_tx, last_tx_change = None, time.monotonic()
        while True:
            time.sleep(STALL_POLL_S)
            try:
                with self.lock:
                    alive = self._mpv_alive()
                    age = time.monotonic() - self.child_started
                if not alive or age < 30:  # startup grace: file/stream open
                    last_pos, last_change = None, time.monotonic()
                    last_tx, last_tx_change = None, time.monotonic()
                    continue
                paused = mpv_get("pause")
                pos = mpv_get("playback-time")
                now = time.monotonic()
                # deliberate pause is not a stall, and sends no audio —
                # the TX clock must not run while paused; an unresponsive
                # IPC (both None) is treated the same as a frozen position
                if paused is True:
                    last_pos, last_change = pos, now
                    last_tx, last_tx_change = None, now
                    continue
                zombie = False
                if pos is not None and pos != last_pos:
                    last_pos, last_change = pos, now
                    # the clock moves — but does anything leave the radio?
                    # (only the bt output routes through the controller)
                    if current_output()["output"] != "bt":
                        last_tx, last_tx_change = None, now
                        continue
                    tx = _bt.hci_tx_bytes()
                    # None = can't judge (no adapter/hciconfig); a lower
                    # value = counter reset or wrap — both restart the clock
                    if tx is None or last_tx is None or tx != last_tx:
                        last_tx, last_tx_change = tx, now
                        continue
                    if now - last_tx_change < STALL_S:
                        continue
                    zombie = True
                    log(f"playback stalled {int(now - last_tx_change)}s "
                        f"(position moves, radio TX flat) — rebuilding the "
                        f"bluetooth link and restarting player")
                else:
                    stalled = now - last_change
                    if stalled < STALL_S:
                        continue
                    log(f"playback stalled {int(stalled)}s (position "
                        f"frozen) — restarting player")
                with self.lock:
                    self._stop_child()  # bookmark survives (terminated flag)
                ready = False
                healed = False
                if zombie:
                    # bluez is lying (the PCM is still listed), so
                    # _audio_ready() would answer yes against a dead link
                    # and we'd respawn straight back into the zombie.
                    # Tear down + reconnect first, THEN trust the probe.
                    healed = True
                    _bt_recover("reconnect")
                for i in range(12):  # give a rebooting speaker ≤60s
                    ready = _audio_ready()
                    if ready:
                        break
                    # same self-heal as the player's racing guard: crash
                    # signature in the kernel log -> recover immediately,
                    # otherwise give a plain speaker dropout 20s first
                    if not healed and (i >= 4 or _bt._hci_crashed()):
                        healed = True
                        log("audio missing — running bluetooth recovery")
                        _bt_recover("ensure")
                    time.sleep(5)
                if not ready:
                    # speaker still gone: don't restart into a void — the
                    # bookmark is saved, any button press resumes later
                    log("output still not ready — leaving playback stopped")
                    last_pos, last_change = None, time.monotonic()
                    last_tx, last_tx_change = None, time.monotonic()
                    continue
                with self.lock:
                    if (self.target and self.source == "mpv"
                            and not self._mpv_alive()):
                        self._spawn(self.target, reverse=self.reverse,
                                    resume=self.resume)
                last_pos, last_change = None, time.monotonic()
                last_tx, last_tx_change = None, time.monotonic()
            except Exception as e:
                log(f"stall watchdog error: {e!r}")

    def _persist(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"target": self.target, "source": self.source,
                       "reverse": self.reverse, "resume": self.resume,
                       "updated": time.time()}, f)
        os.replace(tmp, LAST_FILE)

    def _mpv_alive(self):
        return self.child is not None and self.child.poll() is None

    def _stop_child(self):
        if self._mpv_alive():
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()
        self.child = None

    def _ensure_spotify_backend(self):
        """go-librespot may be parked by the offline supervisor (its tick
        is 60s — far too slow for a play tap). True when the unit is (or
        was just) started, False when there is genuinely no internet so
        the caller can fail FAST instead of a 30s silent session-wait."""
        try:
            if subprocess.run(["systemctl", "is-active", "--quiet",
                               "go-librespot"], timeout=10).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            return True  # can't tell — let the normal path try
        if not _internet_up():
            _SPOT_OFFLINE[0] = True
            return False
        _SPOT_OFFLINE[0] = False
        try:
            subprocess.run(["systemctl", "start", "go-librespot"],
                           timeout=30)
            log("go-librespot was parked — started for the play request")
        except (OSError, subprocess.TimeoutExpired):
            pass
        return True

    def _spawn(self, target, fresh=False, episode=None, reverse=False,
               cache=None, resume=True, exact=False):
        args = [sys.executable, player_path()]
        if fresh:
            args.append("--fresh")
        if not resume:
            args.append("--no-resume")
        if exact:
            args.append("--exact")
        if reverse:
            args.append("--reverse")
        if episode:
            args += ["--episode", episode]
        if cache is not None:
            args += ["--cache", str(cache)]
        args.append(target)
        self.child = subprocess.Popen(args)
        self.child_started = time.monotonic()

    def play(self, target, fresh=False, episode=None, reverse=False,
             cache=None, resume=True):
        with self.lock:
            _kick_bt_connect()  # pressing play = wanting sound NOW
            # Same card back in the slot (or same link replayed): if its
            # session is still loaded, unpause instead of restarting.
            # An explicit episode pick must respawn — the user asked for a
            # specific place in the queue, not "continue".
            if (not fresh and not episode and target == self.target
                    and self.source == "mpv" and self._mpv_alive()):
                try:
                    r = mpv_ipc(["set_property", "pause", False])
                    if r.get("error") == "success":
                        log(f"play (already loaded) -> unpause: {target}")
                        return {"source": "mpv", "target": target,
                                "resumed": True}
                except OSError:
                    pass  # IPC gone but child alive? fall through to respawn
            # Same shortcut for Spotify: a live session for this target
            # continues in place (unpause) — a respawn would reload the
            # context and seek, an audible 2-3s hiccup for nothing.
            if (not fresh and not episode and target == self.target
                    and self.source == "spotify" and is_spotify(target)):
                try:
                    st = go_status()
                    if (st.get("track") or {}) and not st.get("stopped"):
                        if st.get("paused"):
                            go("/player/resume")
                        log(f"play (already loaded) -> resume: {target}")
                        return {"source": "spotify", "target": target,
                                "resumed": True}
                except OSError:
                    pass  # session gone — fall through to respawn (bookmark)
            if is_spotify(target) and not self._ensure_spotify_backend():
                # parked and genuinely offline: say so NOW — spawning a
                # player that waits 30s for a session that cannot come
                # just looks like a dead box (field report)
                log("play: no internet — spotify can't start")
                return {"source": "spotify", "target": target,
                        "error": "no-internet"}
            self._stop_child()
            self._spawn(target, fresh, episode, reverse, cache, resume)
            self.mpv_shuffle = False  # fresh queue plays in order
            self.target = target
            self.reverse = reverse
            self.resume = resume
            self.source = "spotify" if is_spotify(target) else "mpv"
            self.spot_pending = None
            if self.source == "spotify":
                # remember what go-librespot is switching FROM: until the
                # loaded track changes, its /status still describes the
                # previous context and must not reach the now-playing card
                try:
                    pre = (go_status().get("track") or {}).get("uri")
                except Exception:
                    pre = None
                self.spot_pending = {"pre_uri": pre, "at": time.monotonic()}
                _bm_wake.set()  # bookmark even a short session
            self._persist()
            log(f"play [{self.source}] {target}"
                + (f" (episode {episode})" if episode else ""))
            return {"source": self.source, "target": target}

    def _save_volume(self, v):
        """Remember the box volume so player.py can start mpv at it."""
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = VOL_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"volume": v}, f)
            os.replace(tmp, VOL_FILE)
        except OSError:
            pass

    def volume(self, absolute=None, delta=None):
        """One volume knob for the box: set/adjust whatever is active.
        mpv gets its softvol (0-100); Spotify gets go-librespot's volume
        scaled from our 0-100 to its volume_steps."""
        cap = load_settings()["volume_cap"]  # child-safety ceiling
        with self.lock:
            if self._mpv_alive() and self.source == "mpv":
                try:
                    if absolute is None:
                        cur = mpv_get("volume")
                        absolute = (100 if cur is None else cur) + delta
                    v = max(0, min(cap, round(absolute)))
                    r = mpv_ipc(["set_property", "volume", v])
                    if r.get("error") == "success":
                        self._save_volume(v)
                        log(f"volume -> mpv {v}")
                        return {"routed": "mpv", "volume": v}
                except OSError:
                    pass  # child starting up; fall through to spotify
            st = go_status()
            steps = st.get("volume_steps") or 65535
            if absolute is None:
                absolute = (st.get("volume") or 0) * 100 / steps + delta
            v = max(0, min(cap, round(absolute)))
            try:
                go("/player/volume", body={"volume": round(v * steps / 100)})
                self._save_volume(v)
                log(f"volume -> spotify {v}")
                return {"routed": "spotify", "volume": v}
            except OSError:
                log("volume: no active player")
                return {"routed": None, "volume": None}

    def get_volume(self):
        with self.lock:
            if self._mpv_alive() and self.source == "mpv":
                v = mpv_get("volume")
                if v is not None:
                    return {"routed": "mpv", "volume": round(v)}
        st = go_status()
        if st:
            steps = st.get("volume_steps") or 65535
            return {"routed": "spotify",
                    "volume": round((st.get("volume") or 0) * 100 / steps)}
        return {"routed": None, "volume": None}

    def set_output(self, device, fallback=False):
        pcm = OUTPUT_PCMS.get(device)
        if not pcm:
            return None  # handler answers 400
        if fallback and device == "local" and not _i2s_card_present():
            # btwatchd's speaker-away fallback: without a built-in/HAT
            # card there is nothing to fall back TO — keep bt configured
            # so the reconnect logic brings audio back by itself
            return {"skipped": "no built-in sound card", "output":
                    current_output()["output"]}
        if fallback and current_output()["output"] == device:
            # converge anyway: a deferred mpv switch (transport wasn't up
            # when the user flipped the output) applies on this announce
            with self.lock:
                if device == "bt" and _bt_transport_ready():
                    if self._mpv_alive():
                        try:
                            mpv_ipc(["set_property", "audio-device",
                                     f"alsa/{pcm}"])
                            log("output bt: deferred mpv switch applied")
                        except OSError:
                            pass
                    # idempotent: rewrites + restarts only when the config
                    # still points elsewhere (a deferred switch)
                    if _retarget_go_librespot(pcm):
                        log("output bt: deferred go-librespot retarget "
                            "applied")
            return {"unchanged": True, "output": device}
        with self.lock:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(OUT_FILE + ".tmp", "w") as f:
                json.dump({"output": device, "pcm": pcm}, f)
            os.replace(OUT_FILE + ".tmp", OUT_FILE)
            if not fallback:
                # The user asked for the speaker NOW (OUT_FILE already
                # says bt, so the helper checks the right output)
                _kick_bt_connect()
            mpv_switched = False
            if self._mpv_alive():
                if device == "bt" and not _bt_transport_ready():
                    # NEVER point a live mpv at a bluealsa device with no
                    # A2DP transport: it errors the track and skips to the
                    # next, over and over (field: 'jumps between episodes
                    # like crazy'). Record the intent; btwatchd's announce
                    # applies the mpv switch once the transport exists.
                    log("output -> bt: no A2DP transport yet — mpv stays "
                        "on the current device until the speaker is ready")
                else:
                    try:  # mpv can retarget its audio device live
                        mpv_switched = mpv_ipc(
                            ["set_property", "audio-device", f"alsa/{pcm}"]
                        ).get("error") == "success"
                    except OSError:
                        pass
            if device == "bt" and not _bt_transport_ready():
                # same rule as mpv above: don't bounce go-librespot into a
                # device with no transport — the restart's wifi burst lands
                # exactly during AVDTP setup on the SHARED radio (the
                # coexistence load that crashes the Zero's BT firmware)
                restarted = False
            else:
                st = go_status()
                # box-initiated playback only: a phone streaming its own
                # music through the box must not get hijacked into the
                # box's old target after the restart
                spot_was_playing = (spotify_playing(st)
                                    and st.get("play_origin")
                                    in ("go-librespot", "", None))
                restarted = _retarget_go_librespot(pcm)
                if restarted and spot_was_playing and self.target \
                        and is_spotify(self.target):
                    # unlike mpv (live IPC retarget), the restart killed
                    # the session mid-song — bring the music back where
                    # it was (player.py waits for the session, then
                    # resumes from the bookmark). --exact: this is an
                    # interruption, not a re-tap — even 0:08 into a song
                    # must come back at 0:08, or it reads as a restart
                    self._spawn(self.target, resume=self.resume, exact=True)
                    log("output switch: resuming spotify from the bookmark")
            log(f"output -> {device} (pcm {pcm}, "
                f"mpv {'switched' if mpv_switched else 'n/a'}, "
                f"go-librespot {'restarted' if restarted else 'unchanged'})")
            out = {"output": device, "pcm": pcm,
                   "mpv_switched": mpv_switched,
                   "spotify_restarted": restarted}
            if device == "local" and not _i2s_card_present():
                out["warning"] = ("no I2S sound card found — is the HAT "
                                  "mounted and hat-audio-on + reboot done? "
                                  "Playback will be silent until then.")
            return out

    def shuffle(self, enabled):
        """mpv: reshuffle/restore the playlist order (current track keeps
        playing). Spotify: shuffle_context — enabling BEFORE /play makes
        playback start on a random track, so the PWA can pre-arm it."""
        with self.lock:
            if self._mpv_alive() and self.source == "mpv":
                cmd = ["playlist-shuffle"] if enabled else ["playlist-unshuffle"]
                try:
                    if mpv_ipc(cmd).get("error") == "success":
                        self.mpv_shuffle = enabled
                        log(f"shuffle {enabled} -> mpv")
                        return {"routed": "mpv", "shuffle": enabled}
                except OSError:
                    pass
            try:
                go("/player/shuffle_context", body={"shuffle_context": enabled})
                log(f"shuffle {enabled} -> spotify")
                return {"routed": "spotify", "shuffle": enabled}
            except OSError:
                return {"routed": None, "shuffle": None}

    def pause(self):
        """Pause (never toggle) whatever is audible. Used by the card-slot
        switch on card removal: player stays loaded, so re-inserting the
        same card unpauses instantly."""
        with self.lock:
            acted = []
            if self._mpv_alive():
                try:
                    if mpv_ipc(["set_property", "pause", True]).get("error") \
                            == "success":
                        acted.append("mpv")
                except OSError:
                    pass
            if spotify_playing():
                try:
                    go("/player/pause")
                    acted.append("spotify")
                except OSError:
                    pass
            log(f"pause -> {', '.join(acted) if acted else 'nothing playing'}")
            return {"paused": acted}

    def stop(self):
        """Stop = done: also clear the resume bookmark, so the next play
        starts from the top. (Pause / power-off keep the position.)"""
        with self.lock:
            self._stop_child()
            try:
                go("/player/pause")
            except OSError:
                pass
            # Clear ONLY the current target's bookmark: stopping a podcast
            # must not wipe the Spotify playlist's position (or vice versa)
            if self.target and is_spotify(self.target):
                try:
                    _spotify.clear_bookmark(
                        _spotify.to_uri(self.target) or self.target)
                except OSError:
                    pass
            elif self.target:
                try:
                    os.remove(os.path.join(STATE_DIR,
                                           state_key(self.target) + ".json"))
                except OSError:
                    pass
            log("stop (bookmark cleared)")
            return {"stopped": True}

    def command(self, action):
        with self.lock:
            _kick_bt_connect()  # any transport control = sound intent
            # 1) a running mpv session owns the controls
            if self._mpv_alive() and self.source == "mpv":
                try:
                    if action == "prev":
                        # >5s into the episode: restart it (standard player
                        # semantics — also fixes prev being a no-op after a
                        # resume, which rotates the episode to queue slot 0)
                        pos = mpv_get("playback-time")
                        cmd = ["seek", 0, "absolute"] \
                            if isinstance(pos, (int, float)) and pos > 5 \
                            else ["playlist-prev"]
                    else:
                        cmd = {"playpause": ["cycle", "pause"],
                               "next": ["playlist-next"]}[action]
                    if mpv_ipc(cmd).get("error") == "success":
                        log(f"{action} -> mpv")
                        return {"routed": "mpv"}
                except OSError:
                    pass  # child starting up; fall through but don't respawn
            # 2) Spotify actively playing (covers phone-initiated sessions)
            if spotify_playing():
                spotify_command(action)
                self.source = "spotify"
                self._persist()
                log(f"{action} -> spotify (active)")
                return {"routed": "spotify"}
            # 3) last thing used was Spotify -> resume/skip there — but only
            # when a track is actually loaded. After a reboot go-librespot
            # is logged in with an EMPTY session; a playpause into that void
            # "succeeds" silently and the button feels dead. Fall through to
            # rule 4 instead: replay the target, which resumes exactly.
            if self.source == "spotify":
                try:
                    st = go_status()
                    if (st.get("track") or {}) and not st.get("stopped"):
                        spotify_command(action)
                        log(f"{action} -> spotify (last)")
                        return {"routed": "spotify"}
                    log("spotify session is empty — replaying last target")
                except OSError:
                    pass
            # 4) dead session + remembered target -> bring it back (resumes)
            if self.target and not self._mpv_alive():
                if is_spotify(self.target) \
                        and not self._ensure_spotify_backend():
                    log(f"{action}: no internet — spotify can't start")
                    return {"routed": None, "error": "no-internet"}
                self._spawn(self.target, reverse=self.reverse,
                            resume=self.resume)
                log(f"{action} -> resuming last: {self.target}")
                return {"routed": "resume", "target": self.target}
            log(f"{action}: nothing to control")
            return {"routed": None}

    def status(self):
        with self.lock:
            mpv_alive = self._mpv_alive()
            target, source = self.target, self.source
        out = {"source": source, "target": target, "playing": False,
               "title": None, "position": None, "duration": None,
               "artwork": None, "episode_id": None, "shuffle": False,
               "spotify_offline": bool(_SPOT_OFFLINE[0]),
               "output": current_output()["output"]}
        if mpv_alive:
            out["shuffle"] = self.mpv_shuffle
            out["playing"] = mpv_get("pause") is False
            out["title"] = mpv_get("media-title")
            out["position"] = mpv_get("playback-time")
            out["duration"] = mpv_get("duration")  # None = live stream
            try:  # which episode (player.py publishes it; match on path)
                with open(NOW_FILE) as f:
                    now = json.load(f)
                mpath = mpv_get("path")
                q = _queue_map()
                item = (q.get("items") or {}).get(mpath) \
                    if q and q.get("target") == target else None
                if now.get("url") == mpath:
                    out["episode_id"] = now.get("id")
                    out["title"] = now.get("title") or out["title"]
                    out["artwork"] = now.get("image")
                elif item:
                    # mpv advanced (or was skipped) and player.py's publish
                    # is a poll behind — the queue map resolves the LIVE
                    # path instantly, so the new name/art show the same
                    # second the audio changes
                    out["episode_id"] = item.get("id")
                    out["title"] = item.get("title") or out["title"]
                    out["artwork"] = item.get("image")
                elif now.get("target") == target:
                    # Transition: mpv is still loading (no path yet), or
                    # plays something outside the map. Serve the last
                    # published name and art rather than flashing a raw
                    # .mp3 filename and the show cover — media-title is
                    # only kept when it is a real title, not a basename.
                    if (mpath is None or not out["title"]
                            or out["title"] == os.path.basename(mpath)
                            or out["title"] == mpath):
                        out["title"] = now.get("title") or out["title"]
                    out["artwork"] = now.get("image")
            except (OSError, ValueError):
                pass
        st = go_status()
        track = st.get("track") or {}
        sp_playing = spotify_playing(st)
        out["spotify"] = {"playing": sp_playing,
                          "track": track.get("name") or None,
                          "artists": track.get("artist_names") or [],
                          "album": track.get("album_name") or None,
                          "artwork": track.get("album_cover_url") or None}
        # A paused Spotify track is still "what's on" — keep showing it
        # (title/artwork/position) with playing=False, like the mpv side does.
        # Gate on "mpv supplied nothing" rather than "child dead": while a
        # spawn is starting up the socket answers nothing, and blanking the
        # card to 'Nothing playing' for those seconds looks broken.
        # Only when Spotify is actually in charge though (current source, or
        # audibly playing right now): a track parked paused in go-librespot
        # from an EARLIER session must not hijack the card — the play button
        # routes to the current source, and card and button must agree.
        if (out["title"] is None and track and not st.get("stopped")
                and (sp_playing or source == "spotify")):
            out["playing"] = sp_playing
            out["shuffle"] = bool(st.get("shuffle_context"))
            out["source"] = "spotify"
            out["title"] = track.get("name")
            out["duration"] = (track.get("duration") or 0) / 1000 or None
            # position lives on the track object (ms, live-extrapolated)
            out["position"] = (track.get("position") or 0) / 1000
            out["artwork"] = out["spotify"]["artwork"]
        # A freshly tapped spotify target is still loading: go-librespot's
        # /status keeps describing the PREVIOUS context for a few seconds,
        # which put another playlist's cover and title on the card (kids:
        # "wrong picture!"). Until the loaded track actually changes (or
        # 20s passes), present the tapped entry's own identity instead:
        # its bookmark's track + position and its pre-cached mosaic.
        p = self.spot_pending
        if p and source == "spotify" and target and is_spotify(target):
            if ((track.get("uri") and track.get("uri") != p.get("pre_uri"))
                    or time.monotonic() - p["at"] > 20):
                self.spot_pending = None  # the new context took over
            else:
                try:
                    uri = _spotify.to_uri(target)
                    bm = _spotify.read_bookmark(uri) if uri else None
                except OSError:
                    bm = None
                name = (bm or {}).get("name")
                if not name:
                    e = next((e for s in load_library().get("sections", [])
                              for e in s.get("entries", [])
                              if e.get("target") == target), None)
                    name = (e or {}).get("name") or "Spotify"
                out["source"], out["playing"] = "spotify", True
                out["title"] = name
                out["position"] = (bm.get("position") or 0) / 1000 \
                    if bm else None
                out["duration"] = ((bm.get("duration") or 0) / 1000 or None) \
                    if bm else None
                try:  # the entry's own mosaic is pre-cached on disk
                    out["artwork"] = content.collection_image(target) \
                        or (bm or {}).get("artwork")
                except Exception:
                    out["artwork"] = (bm or {}).get("artwork")
        # Ghost sessions: nothing is live, but a bookmarked target is
        # remembered -> present it as paused-at-position instead of
        # "nothing playing". Pressing play resumes exactly there.
        if out["title"] is None and target and is_spotify(target):
            try:
                bm = _spotify.read_bookmark(
                    _spotify.to_uri(target) or target)
            except OSError:
                bm = None
            if bm and bm.get("uri") and (bm.get("position") or 0) > 20000:
                out["playing"] = mpv_alive  # a spawn in flight IS starting
                out["source"] = "spotify"
                out["title"] = bm.get("name")
                out["artwork"] = bm.get("artwork")
                out["position"] = (bm.get("position") or 0) / 1000
                out["duration"] = (bm.get("duration") or 0) / 1000 or None
        if out["title"] is None and target and not is_spotify(target):
            try:
                with open(os.path.join(STATE_DIR,
                                       state_key(target) + ".json")) as f:
                    bk = json.load(f)
            except (OSError, ValueError):
                bk = None
            if bk and bk.get("pos"):
                out["playing"] = mpv_alive  # a spawn in flight IS starting
                out["source"] = "mpv"
                out["position"] = bk.get("pos")
                try:
                    with open(NOW_FILE) as f:
                        now = json.load(f)
                except (OSError, ValueError):
                    now = {}
                if now.get("target") == target:
                    out["title"] = now.get("title")
                    out["artwork"] = now.get("image")
                    out["episode_id"] = now.get("id")
                    out["duration"] = now.get("duration")
                if not out["title"]:
                    out["title"] = os.path.basename(target.rstrip("/"))
        # Stopped-but-remembered: no bookmark (stop cleared it), yet play
        # WILL start this target from the top — say so ("ready at 0:00")
        # instead of pretending nothing exists. Card and button must agree.
        if out["title"] is None and target:
            name = None
            for sec in load_library().get("sections", []):
                for e in sec.get("entries", []):
                    if e.get("target") == target:
                        name = e.get("name")
                        break
                if name:
                    break
            try:
                with open(NOW_FILE) as f:
                    now = json.load(f)
            except (OSError, ValueError):
                now = {}
            if now.get("target") == target:
                name = name or now.get("title")
                out["artwork"] = now.get("image")
            if name:
                out["source"] = "spotify" if is_spotify(target) else "mpv"
                out["title"] = name
                out["position"] = 0
                out["playing"] = mpv_alive  # a spawn in flight IS starting
        # Offline-proof cover for the screen: the episode artwork above is
        # a gfx.nrk.no URL even when the episode itself plays from the
        # local cache — synced shows have cover.jpg on disk, serve that
        # alongside so the box needs no network to show SOMETHING.
        if target and not is_spotify(target):
            try:
                out["artwork_local"] = content.collection_image(target)
            except Exception:
                out["artwork_local"] = None
        out["bt_waiting"], out["bt_ready"], out["bt_lost"] = \
            _bt_wait_state(out["playing"])
        if out["bt_lost"]:
            # the popup's A-option ("play on the box speaker") is only
            # offered where a box speaker exists — BT-only boxes get X
            out["bt_local_ok"] = _i2s_card_present()
        return out


ORCH = Orchestrator()


def _bt_playback_active():
    """Is there an mpv session on the bluetooth output right now?
    netmgmt's wifi probe holds while this is true — an NM scan on the
    shared 2.4GHz radio mid-A2DP stutters the audio and is the documented
    firmware crasher (bt.py recover()). Paused counts too: a kid
    mid-listen resumes any second, and resuming into a live ~30s probe
    window is the same collision — the hold only ends when the session
    is gone (stop, end of queue, idle teardown). Spotify is deliberately
    not checked: the probe only runs with wifi down, where it can't
    stream."""
    try:
        if current_output()["output"] != "bt":
            return False
        with ORCH.lock:
            return ORCH._mpv_alive()
    except Exception:
        return False


_netmgmt.probe_hold[0] = _bt_playback_active


def _net_changed():
    """A wifi SWITCH while online strands go-librespot's long-lived TCP
    connections (AP/dealer/spclient) — they die silently and it spends
    minutes in 30-60s timeout storms that wedge its local API, which
    /status and /playpause block on: the field-reported frozen UI
    (2026-07-17). Restarting is ~5s and deterministic. try-restart:
    a parked unit stays parked — the supervisor owns starting it."""
    log("network changed — restarting go-librespot (stale connections)")
    try:
        subprocess.run(["systemctl", "try-restart", "go-librespot"],
                       timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart after net change failed: {e!r}")


_netmgmt.net_changed[0] = _net_changed


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the journal clean
        pass

    def _send(self, code, obj):
        """Client may hang up while waiting on a long operation (bt pair
        can take a minute) — a dead socket is not an error worth a
        journal traceback."""
        try:
            self._send_unsafe(code, obj)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_unsafe(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, cache=False):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send(404, {"error": "not found"})
            return
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",
                         "max-age=3600" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name):
        """Serve a file from the PWA web dir; True when handled."""
        path = os.path.realpath(os.path.join(WEB_DIR, name))
        if not path.startswith(os.path.realpath(WEB_DIR) + os.sep):
            return False
        if not os.path.isfile(path):
            return False
        self._send_file(path)
        return True

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/status":
            self._send(200, ORCH.status())
        elif url.path == "/volume":
            self._send(200, ORCH.get_volume())
        elif url.path == "/library":
            self._send(200, library_with_covers())
        elif url.path == "/output":
            self._send(200, current_output())
        elif url.path == "/settings":
            self._send(200, load_settings())
        elif url.path == "/system":
            st = system_status()
            try:
                # short timeout: while go-librespot flaps at boot (no DNS
                # yet) a 5s wait here starves /system — the screen sits on
                # its splash even though playback is already running
                st["spotify_user"] = go_status(timeout=1).get("username")
            except OSError:
                st["spotify_user"] = None
            if st.get("spotify_user") is None:  # /status is None while it
                st["spotify_user"] = _spotify.logged_in_user()  # reconnects
            st["spotify_open"] = _spotify.zeroconf_open()
            st["spotify_api"] = _spotify_web.configured()
            self._send(200, st)
        elif url.path == "/bt":
            self._send(200, bt_status())
        elif url.path == "/spotify/profile":
            # Live preview of a profile's public playlists — the PWA calls
            # this to validate a username before saving a follow-section.
            q = urllib.parse.parse_qs(url.query)
            user = _spotify_web.parse_user((q.get("user") or [None])[0])
            if not user:
                self._send(400, {"error": "user required"})
            elif not _spotify_web.configured():
                self._send(503, {"error": (
                    "Spotify API credentials are not set up on this box — "
                    "run install.sh and answer the client id/secret prompt "
                    "(free app at developer.spotify.com/dashboard)")})
            else:
                try:
                    self._send(200, {"user": user, "playlists":
                                     _spotify_web.user_playlists(user)})
                except urllib.error.HTTPError as e:
                    msg = ("no Spotify profile named "
                           f"{user!r}" if e.code == 404
                           else f"Spotify API error {e.code}")
                    self._send(502, {"error": msg})
                except Exception as e:
                    log(f"profile preview failed for {user}: {e!r}")
                    self._send(502, {"error": str(e)})
        elif url.path == "/expand":
            q = urllib.parse.parse_qs(url.query)
            entry_id = (q.get("id") or [None])[0]
            target = (q.get("target") or [None])[0]
            order, name = "auto", None
            if entry_id:
                entry = find_entry(load_library(), entry_id)
                if not entry:
                    self._send(404, {"error": f"no library entry {entry_id}"})
                    return
                target = entry["target"]
                order, name = entry["order"], entry["name"]
            if not target:
                self._send(400, {"error": "id or target required"})
                return
            try:
                self._send(200, expand_target(target, order, name))
            except Exception as e:  # expansion hits the network; stay alive
                log(f"expand failed for {target}: {e!r}")
                self._send(502, {"error": str(e)})
        elif url.path == "/artwork":
            path = (urllib.parse.parse_qs(url.query).get("path") or [None])[0]
            if not path:
                self._send(400, {"error": "path required"})
            elif not artwork_allowed(path):
                self._send(403, {"error": "path not allowed"})
            else:
                self._send_file(path, cache=True)
        elif url.path == "/":
            if not self._static("index.html"):
                self._send(404, {"error": "PWA files not installed"})
        elif "/" not in url.path[1:] and self._static(url.path[1:]):
            pass  # /app.js, /style.css, /manifest.json ...
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n)) if n else {}
        except ValueError:
            self._send(400, {"error": "invalid json"})
            return
        if self.path == "/library":
            try:
                lib = normalize_library(body)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            save_library(lib)
            log(f"library updated ({sum(len(s['entries']) for s in lib['sections'])} entries)")
            # Free the disk held by entries just removed (or flipped to 'no
            # offline'): only entries that still want offline copies keep them.
            try:
                keep = [e["target"] for s in lib["sections"] for e in s["entries"]
                        if e.get("cache")]
                gone = content.prune_cache(keep)
                if gone:
                    log(f"cache: pruned {len(gone)} orphaned offline "
                        f"cache(s): {', '.join(gone)}")
            except Exception as e:  # cleanup must never fail the save
                log(f"cache prune failed: {e!r}")
            _sync_wake.set()  # start caching new/changed entries right away
            self._send(200, lib)
        elif self.path == "/settings":
            try:
                self._send(200, update_settings(body))
            except ValueError as e:
                self._send(400, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n)) if n else {}
        except ValueError:
            body = {}
        try:
            if self.path == "/play":
                target = body.get("target")
                reverse = False
                cache = None  # None = legacy behaviour for raw targets
                resume = True  # 'from start' entries turn this off
                if not target and body.get("id"):
                    entry = find_entry(load_library(), body["id"])
                    if not entry:
                        self._send(404, {"error": f"no library entry {body['id']}"})
                        return
                    target = entry["target"]
                    # Play in the same order the menu showed the episodes
                    reverse = (entry["order"] != "auto"
                               and entry["order"] != _natural_order(target))
                    cache = entry.get("cache", 0)
                    resume = entry.get("resume", True)
                if not target:
                    self._send(400, {"error": "target or id required"})
                    return
                self._send(200, ORCH.play(target, bool(body.get("fresh")),
                                          body.get("episode") or None, reverse,
                                          cache, resume))
            elif self.path in ("/playpause", "/next", "/prev"):
                self._send(200, ORCH.command(self.path[1:]))
            elif self.path == "/pause":
                self._send(200, ORCH.pause())
            elif self.path == "/shuffle":
                if not isinstance(body.get("enabled"), bool):
                    self._send(400, {"error": "enabled (bool) required"})
                    return
                self._send(200, ORCH.shuffle(body["enabled"]))
            elif self.path == "/volume":
                if body.get("volume") is None and body.get("delta") is None:
                    self._send(400, {"error": "volume or delta required"})
                    return
                self._send(200, ORCH.volume(absolute=body.get("volume"),
                                            delta=body.get("delta")))
            elif self.path == "/output":
                r = ORCH.set_output(body.get("device"),
                                    fallback=bool(body.get("fallback")))
                if r is None:
                    self._send(400, {"error":
                                     f"device must be one of {sorted(OUTPUT_PCMS)}"})
                    return
                self._send(200, r)
            elif self.path == "/system/wifi":
                if not isinstance(body.get("enabled"), bool):
                    self._send(400, {"error": "enabled (bool) required"})
                    return
                self._send(200, set_wifi(body["enabled"]))
            elif self.path == "/system/shutdown":
                self._send(200, shutdown(bool(body.get("restart"))))
            elif self.path == "/spotify/logout":
                # the bookmarks belong to the old account
                _spotify.clear_all_bookmarks()
                r = _spotify.logout()
                self._send(200 if r.get("ok") else 500, r)
            elif self.path == "/library/section-logo":
                # Upload (base64/data-URI) or remove (data: null) a home-
                # screen logo for one section. The PWA downsizes client-side.
                sid = str(body.get("id") or "")
                lib = load_library()
                sec = next((s for s in lib["sections"] if s["id"] == sid),
                           None)
                if not sec:
                    self._send(404, {"error": f"no section {sid!r}"})
                    return
                path = os.path.join(ART_DIR, f"section-{sid}.jpg")
                data = body.get("data")
                if not data:  # remove the logo
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    sec.pop("image", None)
                else:
                    try:
                        b64 = data.split(",", 1)[1] \
                            if data.startswith("data:") else data
                        raw = base64.b64decode(b64, validate=True)
                    except (ValueError, AttributeError):
                        self._send(400, {"error": "invalid image data"})
                        return
                    if not 100 <= len(raw) <= 3_000_000:
                        self._send(400, {"error": "image must be 100B-3MB"})
                        return
                    os.makedirs(ART_DIR, exist_ok=True)
                    with open(path + ".tmp", "wb") as f:
                        f.write(raw)
                    os.replace(path + ".tmp", path)
                    sec["image"] = path
                save_library(normalize_library(lib))
                log(f"section logo {'set' if data else 'removed'}: {sid}")
                self._send(200, lib)
            elif self.path == "/wifi/hotspot":
                if not isinstance(body.get("enabled"), bool):
                    self._send(400, {"error": "enabled (bool) required"})
                    return
                if body["enabled"]:
                    ok = start_hotspot()
                    self._send(200, {"ok": ok, "ssid": HOTSPOT_SSID,
                                     "password": HOTSPOT_PSK})
                else:
                    stop_hotspot()
                    self._send(200, {"ok": True})
            elif self.path == "/wifi/scan":
                r = wifi_scan()
                self._send(409 if r is None else 200,
                           r or {"error": "wifi operation already in progress"})
            elif self.path in ("/wifi/connect", "/wifi/forget",
                               "/wifi/add"):
                ssid = str(body.get("ssid") or "").strip()
                if not ssid or len(ssid) > 32:
                    self._send(400, {"error": "ssid required (max 32 chars)"})
                    return
                pw = str(body["password"]) if body.get("password") else None
                if self.path == "/wifi/connect":
                    r = wifi_connect(ssid, pw)
                elif self.path == "/wifi/add":
                    r = wifi_add(ssid, pw)
                else:
                    r = wifi_forget(ssid)
                self._send(409 if r is None else 200,
                           r or {"error": "wifi operation already in progress"})
            elif self.path == "/bt/scan":
                resume = _bt_quiesce()  # discovery makes A2DP stutter badly
                r = bt_scan()
                _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/bt/pair":
                args = ["connect"]
                if body.get("name"):
                    args.append(str(body["name"]))
                resume = _bt_quiesce()
                r = bt_action(args, timeout=120)
                _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/bt/lost":
                # internal: btwatchd's transport-died hint (see
                # _bt_transport_lost — guarded, safe on duplicates)
                self._send(200, _bt_transport_lost())
            elif self.path == "/bt/visible":
                try:
                    secs = min(max(int(body.get("secs") or 120), 10), 300)
                except (TypeError, ValueError):
                    secs = 120
                # an incoming SSP dance during A2DP streaming is the same
                # firmware crasher as an outgoing pair — quiesce around it
                resume = _bt_quiesce()
                r = bt_action(["visible", str(secs)], timeout=secs + 150)
                _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path in ("/bt/connect", "/bt/forget",
                               "/bt/disconnect"):
                mac = str(body.get("mac") or "")
                if not MAC_RE.match(mac):
                    self._send(400, {"error": "valid mac required"})
                    return
                cmd = {"/bt/connect": "use", "/bt/forget": "forget",
                       "/bt/disconnect": "disconnect"}[self.path]
                resume = _bt_quiesce() if cmd == "use" else False
                r = bt_action([cmd, mac], timeout=90 if cmd == "use" else 30)
                if cmd == "use":
                    _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/stop":
                self._send(200, ORCH.stop())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never let one request kill the daemon
            log(f"error on {self.path}: {e!r}")
            self._send(500, {"error": str(e)})


def _bt_quiesce():
    """Connecting/pairing WHILE A2DP streams crashes the Zero 2 W's BT
    firmware outright (kernel: 'hardware error 0x00' — seen in the field
    when adding headset #2 mid-play). Silence the radio first; the caller
    resumes afterwards and the bookmark makes it seamless."""
    resume = False
    with ORCH.lock:
        if ORCH._mpv_alive():
            resume = True
            log("bt connect: stopping playback first (firmware safety)")
            ORCH._stop_child()  # bookmark survives; we resume after
    try:
        if spotify_playing():
            resume = True
            go("/player/pause")
    except OSError:
        pass
    return resume


def _bt_resume(resume):
    if not resume:
        return
    with ORCH.lock:
        target, reverse, resume = ORCH.target, ORCH.reverse, ORCH.resume
        if target and not ORCH._mpv_alive():
            log("bt connect done — resuming playback on the new output")
            ORCH._spawn(target, reverse=reverse, resume=resume)


def _wifi_boot_reenable():
    """'Wifi off' in the PWA rfkill-blocks the radio, and systemd-rfkill
    restores that block across reboots — a headless box would stay dark
    and unreachable forever. Make the switch session-only: a power cycle
    always brings wifi (and with it the PWA) back."""
    try:
        enabled, _ssid, _ip = wifi_state()
        if not enabled:
            log("wifi was left off — re-enabling on startup")
            set_wifi(True)
    except Exception as e:
        log(f"wifi boot re-enable failed: {e!r}")


def _spotify_bookmarker():
    """Spotify's cloud remembers positions for ITS clients only — so we
    bookkeep like we do for mpv: while Spotify plays, snapshot the track,
    position and (when the box started it) the context every few seconds.
    play {uri, skip_to_uri} + seek replays it exactly, queue intact.
    The per-tick accept rules (box-initiated only, per-context files) live
    in spotify.bookmark_step/save_bookmark."""
    interval = 5
    while True:
        woke = _bm_wake.wait(interval)
        _bm_wake.clear()
        try:
            st = go_status()
            track = st.get("track") or {}
            # power hygiene: with no session at all there is nothing to
            # bookkeep — drop to a 30s heartbeat instead of waking the CPU
            # 12x/min around the clock. A live (even paused) session keeps
            # the 5s cadence so resume stays accurate.
            interval = 30 if (not track or st.get("stopped")) else 5
            if woke:
                interval = 5  # a play was just issued — watch closely
            if ORCH.source == "mpv" and ORCH._mpv_alive():
                # mpv owns playback but spotify still reports playing: this
                # is the switch race — /play set target+source to the mpv
                # target instantly, while player.py takes a moment to pause
                # spotify. Writing now would stamp the wrong context over a
                # perfectly resumable bookmark. Skip the tick.
                continue
            context = None
            if ORCH.source == "spotify" and ORCH.target \
                    and is_spotify(ORCH.target):
                context = _spotify.to_uri(ORCH.target)
            bm = _spotify.bookmark_step(st, context)
            if bm is not None:
                _spotify.save_bookmark(bm)
        except Exception:
            pass


def _audio_ready():
    return audio_ready()  # shared logic lives in tapbox.output


def _bt_recover(verb):
    """Run a bt.py recovery verb ('ensure' or 'reconnect') as a
    subprocess — it takes the cross-process radio lock there, so a
    btwatchd retry can't race the recovery mid-flight."""
    try:
        subprocess.run([sys.executable, _bt.__file__, verb],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"bluetooth recovery ({verb}) failed: {e!r}")


def _bt_transport_ready():
    """Does the configured speaker have a live A2DP PCM right now?"""
    try:
        with open(_bt.MAC_FILE) as f:
            mac = f.read().strip()
        return bool(mac) and btbus.a2dp_pcm_present(mac)
    except OSError:
        return False


_BT_HEAL = {"lock": threading.Lock(), "last": 0.0}
BT_HEAL_COOLDOWN_S = float(os.environ.get("TAPBOX_BT_HEAL_COOLDOWN", "300"))


def _heal_crashed_controller():
    """btwatchd is deliberately passive on adapter loss (PLAN-bt-dbus.md
    §1), so a kick can't fix a CRASHED firmware — its Connect just keeps
    failing NotReady. Field log 2026-07-17: 'hardware error 0x00' left
    the speaker dead indefinitely, because playback fell back to the
    local output and the stall watchdog (the only other healer) never
    saw a stall. So play intent itself checks the crash signature and
    runs recovery in the background — cheap when healthy (one hciconfig
    ioctl; the kernel journal is only read when the controller is down),
    deduped by the non-blocking lock and cooldown-guarded so button
    mashing can't stack recoveries. After a successful recovery the
    bluetooth restart re-enters btwatchd's fast window on its own; the
    extra kick just shaves the last seconds off."""
    if not _BT_HEAL["lock"].acquire(blocking=False):
        return  # a recovery is already running
    try:
        if time.monotonic() - _BT_HEAL["last"] < BT_HEAL_COOLDOWN_S:
            return  # recently tried — a wedge needing a power cycle
        if not _bt._hci_crashed():
            return  # plain speaker-away: btwatchd's job, not ours
        _BT_HEAL["last"] = time.monotonic()
        log("play intent found a crashed BT controller — recovering")
        _bt_recover("recover")
        try:
            with open(_bt.KICK_FILE + ".tmp", "w") as f:
                f.write(str(time.time()))
            os.replace(_bt.KICK_FILE + ".tmp", _bt.KICK_FILE)
        except OSError:
            pass
    except Exception as e:  # a dead healer = the field bug comes back
        log(f"bt heal error: {e!r}")
    finally:
        _BT_HEAL["lock"].release()


# the box screen's speaker popups (field log 2026-07-17: the speaker came
# up 25s before anyone pressed play again — nobody KNEW it was ready).
# since>0 = a play attempt hit a disconnected speaker ("not connected,
# waiting..." popup); lost>0 = the speaker DIED mid-play and we stopped
# the player ("disconnected — X: reconnect, A: play on the box speaker");
# when the transport then shows up, either flips to a short "connected —
# press play" window. All consumed via /status.
_BT_WAIT = {"since": 0.0, "ready_until": 0.0, "lost": 0.0}
BT_WAIT_S = float(os.environ.get("TAPBOX_BT_WAIT_S", "180"))
BT_READY_FLASH_S = float(os.environ.get("TAPBOX_BT_READY_FLASH", "20"))
# auto-resume window after an auto-stop. 150s (not 30): a speaker OFF/ON
# cycle takes 20-60s to re-establish A2DP (own reconnect flaps during its
# boot, btwatchd's ladder runs 20-40s) — field log 2026-07-17 19:02 landed
# at 51s and got the press-A popup instead of just continuing. Within the
# popup's own lifetime the loss is recent and someone is present; beyond
# BT_WAIT_S the lost state has expired and NOTHING resumes by itself.
BT_RESUME_S = float(os.environ.get("TAPBOX_BT_RESUME_S", "150"))


def _bt_blip_resume():
    """The speaker came back within seconds of dying mid-play — resume
    by itself, like headphones against a phone: a blip is the CODE's
    problem, not the kid's (no 'press A' homework for a 5s dropout).
    Outside the blip window the popup's 'press A' stays — blasting
    audio when a speaker reappears an hour later is wrong the other
    way. Same respawn guard as the stall watchdog: if the kid meanwhile
    resumed, stopped or switched output, this is a no-op. Spotify needs
    its output REBUILT first (see _go_output_rebuild) — a plain resume
    plays silently into the dead ALSA handle — then the same spawn path
    replays from the spotify bookmark."""
    with ORCH.lock:
        source, target = ORCH.source, ORCH.target
        if (target and source == "mpv"
                and not ORCH._mpv_alive()):
            log("speaker back within the blip window — resuming")
            ORCH._spawn(target, reverse=ORCH.reverse,
                        resume=ORCH.resume)
            return
    if source == "spotify" and target:
        _go_output_rebuild()
        with ORCH.lock:
            if ORCH.target == target and not ORCH._mpv_alive():
                log("speaker back within the blip window — resuming spotify")
                ORCH._spawn(target, reverse=ORCH.reverse,
                            resume=ORCH.resume)


def _bt_transport_lost():
    """btwatchd's transport-died notification. If mpv is playing into
    the dead speaker, every episode now ERRORS and auto-advances (field
    log 2026-07-17: ~15 episodes skipped in 3s — the stall watchdog
    can't see it, the position is moving). Stop the player — the 3s
    bookmark preserves the exact episode/position, the same trick the
    stall watchdog uses — and arm the screen's choice popup. Spotify
    plays via go-librespot, not an mpv child: there its ALSA output just
    died under it ('output device failed' in its log, the track burning
    on silently) — pause it instead, same popup, and the spotify
    bookmarker keeps the position. Guarded: a drop for a speaker we're
    not playing into is a no-op, so a stale or duplicate notification
    can never kill local playback."""
    if current_output()["output"] != "bt":
        return {"stopped": False}
    with ORCH.lock:
        if ORCH._mpv_alive():
            log("bt transport lost mid-play — stopping (bookmark survives)")
            ORCH._stop_child()
            _BT_WAIT["lost"] = time.monotonic()
            return {"stopped": True}
    try:
        if spotify_playing():
            log("bt transport lost mid-play — pausing spotify")
            go("/player/pause")
            _BT_WAIT["lost"] = time.monotonic()
            _BT_WAIT["lost_spotify"] = True
            return {"stopped": True}
    except OSError:
        pass  # go-librespot unreachable = nothing playing through it
    return {"stopped": False}


def _go_output_rebuild():
    """go-librespot's ALSA output dies WITH the bt transport
    ('snd_pcm_recover: No such device') and STAYS dead: a later
    /player/resume resumes the SESSION but never reopens the device —
    'playing' with no sound (field log 2026-07-17 19:21; two output
    toggles 'fixed' it only because the toggle restarts the service).
    Restart rebuilds the output; the session comes back empty, which
    routes any resume through the proven replay-last path. Wait for the
    login so a replay right after doesn't race the API."""
    log("rebuilding go-librespot's audio output (restart)")
    try:
        subprocess.run(["systemctl", "restart", "go-librespot"],
                       timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot output rebuild failed: {e!r}")
        return
    for _ in range(20):
        try:
            if go_status().get("username"):
                break
        except OSError:
            pass
        time.sleep(1)


def _bt_wait_state(playing):
    """(bt_waiting, bt_ready, bt_lost) for /status. The transport probe
    runs only while a wait/lost is pending — bounded to BT_WAIT_S — so
    the screen's 1/s status poll costs nothing extra in steady state."""
    now = time.monotonic()
    if _BT_WAIT["lost"]:
        if playing or now - _BT_WAIT["lost"] > BT_WAIT_S:
            # resumed (any output — the popup's play-on-box-speaker
            # choice ends in playing) or expired. Deliberately NOT
            # cleared on an output change: the follow-the-speaker
            # fallback flips to local ~23s after every drop on boxes
            # with a built-in speaker, and clearing here silently
            # disarmed the popup and the auto-resume promise.
            _BT_WAIT["lost"] = 0.0
        elif _bt_transport_ready():
            blip = now - _BT_WAIT["lost"] <= BT_RESUME_S
            spot = _BT_WAIT.pop("lost_spotify", False)
            _BT_WAIT["lost"] = 0.0
            if blip:  # short dropout: resume silently, no popup homework
                threading.Thread(target=_bt_blip_resume,
                                 daemon=True).start()
            else:     # speaker back much later: "press A to play"
                if spot:
                    # rebuild NOW so the kid's press-A lands on a fresh
                    # output (a resume into the dead handle is silent)
                    threading.Thread(target=_go_output_rebuild,
                                     daemon=True).start()
                _BT_WAIT["ready_until"] = now + BT_READY_FLASH_S
    if _BT_WAIT["since"]:
        if now - _BT_WAIT["since"] > BT_WAIT_S:
            _BT_WAIT["since"] = 0.0  # stale intent: kid walked away
        elif _bt_transport_ready():
            _BT_WAIT["since"] = 0.0
            _BT_WAIT["ready_until"] = now + BT_READY_FLASH_S
        else:
            return True, False, bool(_BT_WAIT["lost"])
    if playing:
        _BT_WAIT["ready_until"] = 0.0  # they pressed play — popup done
        return False, False, False
    return False, now < _BT_WAIT["ready_until"], bool(_BT_WAIT["lost"])


def _kick_bt_connect():
    """Play intent while the BT speaker has no transport: poke btwatchd
    to attempt a connect right away instead of waiting out its blind-retry
    backoff — up to 300s of silence after a boot where the speaker came
    on late. No-op on the built-in output or with the speaker connected."""
    if current_output()["output"] != "bt" or _bt_transport_ready():
        return
    _BT_WAIT["since"] = time.monotonic()  # the screen shows "waiting..."
    try:
        with open(_bt.KICK_FILE + ".tmp", "w") as f:
            f.write(str(time.time()))
        os.replace(_bt.KICK_FILE + ".tmp", _bt.KICK_FILE)
        log("speaker not connected — kicked btwatchd to connect it now")
    except OSError:
        pass
    # a kick alone can't help a crashed controller — check off-thread
    # (zero added latency on the button) and self-heal if needed
    threading.Thread(target=_heal_crashed_controller, daemon=True).start()


def _internet_up():
    """Actual-internet probe (not just wifi association): plain IP, no
    DNS to hang on — same test player.py's offline filter uses."""
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
        return True
    except OSError:
        return False


def _spotify_supervisor():
    """go-librespot is useless without internet, but restarts forever —
    each round costs ~1s of Zero CPU and journal noise. Park the unit
    while the box is offline; it is back within a minute of
    connectivity returning. Manual restarts while offline (e.g. an
    output switch rewrote its config) get re-parked on the next tick."""
    parked = False
    misses = 0
    while True:
        time.sleep(20)  # a cheap TCP probe; 60s made "no internet" and
        # the recovery lag a button-press generation behind reality
        try:
            if _internet_up():
                misses = 0
                _SPOT_OFFLINE[0] = False
                if parked:
                    subprocess.run(["systemctl", "start", "go-librespot"],
                                   timeout=30)
                    log("spotify: internet is back — go-librespot started")
                    parked = False
                # Once an account is on, close the open Connect door so a
                # passing phone can't overwrite our login. No-op when
                # already locked or not logged in.
                if _spotify.lock():
                    log("spotify: locked to the logged-in account "
                        "(zeroconf closed — box can't be hijacked)")
            else:
                misses += 1
                if misses < 2:
                    # ONE missed probe is not "offline": btwatchd paging
                    # an absent speaker congests the shared 2.4GHz radio
                    # enough to time out the 2s probe — field log
                    # 2026-07-17 19:08: a false 'No internet' banner and
                    # go-librespot park/start churn mid-Spotify, from
                    # nothing but a switched-off headset
                    continue
                _SPOT_OFFLINE[0] = True
                subprocess.run(["systemctl", "stop", "go-librespot"],
                               timeout=30)
                if not parked:
                    log("spotify: no internet — go-librespot parked "
                        "(auto-starts when connectivity returns)")
                    parked = True
        except Exception as e:
            log(f"spotify supervisor error: {e!r}")


def _flag_was_playing():
    """At shutdown (SIGTERM from systemd), record whether something was
    audibly playing — boot resume only continues in that case, so a box
    that was OFF/paused never surprises anyone by blasting on power-on."""
    try:
        playing = False
        if ORCH.child is not None and ORCH.child.poll() is None:
            p = mpv_get("pause")
            if p is not None:
                playing = p is False
            else:
                # systemd TERMs the whole cgroup at once — mpv may already
                # be gone. player.py published the pause state to a file
                # for exactly this moment.
                try:
                    with open(NOW_FILE) as f:
                        # missing key (file from an older player) = playing
                        playing = not json.load(f).get("paused", False)
                except (OSError, ValueError):
                    playing = True  # child alive, no info: assume playing
        if not playing:
            playing = spotify_playing()
        with open(LAST_FILE) as f:
            last = json.load(f)
        last["was_playing"] = bool(playing)
        with open(LAST_FILE + ".tmp", "w") as f:
            json.dump(last, f)
        os.replace(LAST_FILE + ".tmp", LAST_FILE)
    except Exception:
        pass


def _on_term(*_args):
    _flag_was_playing()
    os._exit(0)


def _boot_resume():
    """Power on -> the story continues where it stopped (setting-gated).
    Both mpv content and Spotify resume at the exact second via their
    bookmarks (player.py replays the Spotify context with skip_to_uri
    + seek from the per-context bookmark)."""
    if not load_settings().get("resume_on_boot"):
        return
    try:
        with open(LAST_FILE) as f:
            last = json.load(f)
    except (OSError, ValueError):
        return
    if not last.get("was_playing") or not last.get("target"):
        return
    last["was_playing"] = False  # one attempt per shutdown
    try:
        with open(LAST_FILE + ".tmp", "w") as f:
            json.dump(last, f)
        os.replace(LAST_FILE + ".tmp", LAST_FILE)
    except OSError:
        return
    target = last["target"]
    log(f"boot resume: waiting for the audio path, then continuing {target}")
    for _ in range(45):  # up to ~90s for the BT speaker to reconnect
        if _audio_ready():
            break
        time.sleep(2)
    else:
        log("boot resume: audio path never came up — press play to resume")
        return
    if is_spotify(target):
        # go-librespot must be up AND logged in, or the play call dies
        for _ in range(30):
            if go_status().get("username"):
                break
            time.sleep(2)
        else:
            log("boot resume: go-librespot never became ready — skipping")
            return
    else:
        # Give wifi a moment: without it the player's offline filter drops
        # stream URLs and playback starts at the wrong (cached-only) place.
        # A genuinely offline box proceeds after the wait — cached content
        # is then the RIGHT thing to play.
        for _ in range(15):  # up to ~30s
            try:
                socket.create_connection(("1.1.1.1", 443), timeout=2).close()
                break
            except OSError:
                time.sleep(2)
    ORCH.play(target, reverse=bool(last.get("reverse")),
              resume=bool(last.get("resume", True)))


class PortalHandler(BaseHTTPRequestHandler):
    """Port-80 helper: redirects everything to the PWA. On the setup
    hotspot, wildcard DNS (dnsmasq-shared.d) sends the phone's captive
    probes here — a redirect instead of the expected 204/Success makes
    the phone pop its 'sign in to network' sheet with the PWA in it.
    On the home LAN it doubles as http://tapbox.local -> the PWA."""

    def log_message(self, *args):
        pass

    def _redirect(self):
        host = self.request.getsockname()[0]  # our address on that network
        self.send_response(302)
        self.send_header("Location", f"http://{host}:{PORT}/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_HEAD = _redirect


def _portal_server():
    try:
        srv = ThreadingHTTPServer((BIND, PORTAL_PORT), PortalHandler)
    except OSError as e:
        log(f"portal on :{PORTAL_PORT} not started ({e}) — captive portal off")
        return
    log(f"portal redirect on :{PORTAL_PORT}")
    srv.serve_forever()


def _prewarm_mpv():
    """The first mpv launch of a boot cold-loads mpv + the ffmpeg stack
    (tens of MB) from the SD card at the powersave clock — field log
    2026-07-17: 11s of silence between the player's 'resuming ...' and
    the first audio, with an impatient second press pausing it right as
    it started. Page the libraries in once while the box is idle; later
    launches hit the page cache (~2-3s). The delay keeps it out of the
    boot rush (boot resume, service starts) — the point is warming the
    cache BEFORE the first human play, not during boot I/O."""
    time.sleep(15)
    try:
        subprocess.run(["mpv", "--version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=120)
        log("mpv prewarmed (libraries paged in)")
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"mpv prewarm failed: {e!r}")


def main():
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except ValueError:
        pass  # not the main thread (tests run main() in a thread)
    threading.Thread(target=_boot_resume, daemon=True).start()
    threading.Thread(target=_prewarm_mpv, daemon=True).start()
    threading.Thread(target=_cache_sweeper, daemon=True).start()
    threading.Thread(target=_spotify_bookmarker, daemon=True).start()
    _wifi_boot_reenable()
    threading.Thread(target=_wifi_watchdog, daemon=True).start()
    threading.Thread(target=_battery_runtime_tracker, daemon=True).start()
    threading.Thread(target=_spotify_supervisor, daemon=True).start()
    threading.Thread(target=_portal_server, daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log(f"listening on {BIND}:{PORT} (PWA: http://tapbox.local:{PORT})")
    server.serve_forever()


if __name__ == "__main__":
    main()
