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
  POST /wifi/reconnect {"secs"?} — on-demand: unblock the radio and wait
                      for a known network to join (offline-Spotify X); on
                      success clears spotify_offline + unparks go-librespot
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
  POST /bt/rename  {"mac", "name"} — custom display name (blank resets);
                   shows in the PWA + on the screen (BlueZ Device1.Alias)

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
import json
import mimetypes
import os
import signal
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
from tapbox.paths import (  # noqa: E402
    ART_DIR, RUN_DIR, STATE_DIR, go_restarted_within, note_go_restart)

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
# resume-position display hold (see Orchestrator._settle_position): a
# bookmark below RESUME_MIN_S is never resumed, so nothing to hold; the
# hold releases once live is within TOL of the target, and never lasts
# longer than MAX_S after spawn
RESUME_MIN_S = float(os.environ.get("TAPBOX_RESUME_MIN", "20"))
POSITION_SETTLE_MAX_S = float(os.environ.get("TAPBOX_SETTLE_MAX", "20"))
POSITION_SETTLE_TOL_S = float(os.environ.get("TAPBOX_SETTLE_TOL", "3"))
# boot resume: silent grace before the speaker popup, and the wait tick
BOOT_GRACE_S = float(os.environ.get("TAPBOX_BOOT_GRACE", "25"))
BOOT_TICK_S = float(os.environ.get("TAPBOX_BOOT_TICK", "2"))
# a spotify session that reads 'empty' right after a timed-out control is
# very likely a SLOW TRACK LOAD, not a finished album — hold skips off the
# replay fallback for this long after any control timeout
SPOT_TIMEOUT_HOLD_S = float(os.environ.get("TAPBOX_SPOT_TIMEOUT_HOLD", "30"))
# and even without a recent timeout, re-read an 'empty' session after this
# beat before concluding the album truly ended (mid-load blips resolve)
EMPTY_RECHECK_S = float(os.environ.get("TAPBOX_EMPTY_RECHECK", "2"))
# how long after a spawn to trust player.py's published paused-state while
# mpv's IPC socket is still coming up (the ~1-3s tap->audio window), so the
# screen shows 'playing' at once instead of a dead card
MPV_START_GRACE_S = float(os.environ.get("TAPBOX_MPV_START_GRACE", "12"))
# how long after the daemon starts to prewarm mpv's decode path (idle, off
# the boot rush) so the first human play hits a warm page cache
PREWARM_DELAY_S = float(os.environ.get("TAPBOX_PREWARM_DELAY", "15"))
WEB_DIR = os.environ.get("TAPBOX_WEB") or (
    os.path.join(_here, "web") if os.path.isdir(os.path.join(_here, "web"))
    else "/usr/share/tapbox/web")


def _tick(seconds):
    """All background loops wait through this seam. Tests monkeypatch
    daemon._tick to drive loops deterministically — patching the global
    time.sleep also hit OTHER live daemon threads (they stole scripted
    ticks and the fake could raise inside the arbiter), a real flake
    (QA review Q2)."""
    time.sleep(seconds)


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def player_path():
    p = os.path.join(_here, "player.py")
    return p if os.path.exists(p) else "/usr/local/bin/tapbox-player"


# --- moved to the tapbox package; aliases keep internal call sites and the
# --- tests' daemon.<name> monkeypatching working unchanged ----------------------

from tapbox import bt as _bt, btbus, netmgmt as _netmgmt  # noqa: E402
from tapbox import library as _library  # noqa: E402 — BUSY_CHECK wiring
from tapbox import radio as _radio  # noqa: E402 — shared-radio yield markers
from tapbox.library import (  # noqa: E402
    artwork_allowed, expand_target, find_entry, library_with_covers,
    load_library, normalize_library, save_library, state_key, _cache_sweeper,
    _natural_order, _sync_wake)
from tapbox.netmgmt import (  # noqa: E402
    HOTSPOT_PSK, HOTSPOT_SSID, set_wifi, start_hotspot,
    stop_hotspot, wifi_add, wifi_connect, wifi_forget, wifi_reconnect,
    wifi_scan, wifi_state, _wifi_watchdog)
from tapbox.output import (  # noqa: E402
    OUTPUT_PCMS, OUT_FILE, audio_ready, current_output, _i2s_card_present,
    reopen_go_output, _retarget_go_librespot)
from tapbox import sysinfo as _sysinfo  # noqa: E402 — cache-size invalidation
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
        # last spotify control timeout; far past, NOT 0.0 — on a young
        # monotonic clock 0.0 would read as 'timed out seconds ago'
        self._spot_cmd_timeout_at = -1e9
        self._crash_respawns = 0  # crashed-child heals this boot (max 2)
        threading.Thread(target=self._arbiter, daemon=True).start()
        threading.Thread(target=self._stall_watchdog, daemon=True).start()

    def _arbiter(self):
        """The box stays Spotify Connect-discoverable while mpv plays; if the
        user picks it from the phone mid-podcast, both would fight over the
        BT output. Watch for that takeover and yield mpv gracefully (its
        bookmark is saved, so the card resumes later).

        Two guards keep this from firing on the box's OWN Spotify (self.child
        is player.py for spotify targets too, so 'child alive + spotify
        playing' is NOT proof of a phone): only when the current source is
        mpv (a podcast is what's playing, so a Spotify session appearing IS
        an intrusion), AND the session carries a non-box play_origin. Without
        them the box's boot-resume into a Spotify playlist logged a phantom
        'spotify took over (phone)' and killed its own player (field
        2026-07-20 08:18:39)."""
        while True:
            _tick(4)
            try:
                with self.lock:
                    alive = self._mpv_alive()
                    source = self.source
                    age = time.monotonic() - self.child_started
                # only a podcast/local session can BE taken over; the box's
                # own Spotify child is not a takeover of anything
                if not alive or source != "mpv" or age < 10:
                    continue
                # grace period covered by age>=10s: player.py pauses spotify
                # right after starting; don't mistake that brief overlap
                st = go_status()
                origin = st.get("play_origin")
                phone = spotify_playing(st) and origin not in (
                    "go-librespot", "", None)
                if phone:
                    with self.lock:
                        if self._mpv_alive() and self.source == "mpv":
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
        crashed_since = 0.0  # first poll that saw the crashed child dead
        while True:
            _tick(STALL_POLL_S)
            try:
                with self.lock:
                    alive = self._mpv_alive()
                    age = time.monotonic() - self.child_started
                if not alive or age < 30:  # startup grace: file/stream open
                    crashed_since = (self._heal_crashed_child(crashed_since)
                                     if not alive else 0.0)
                    last_pos, last_change = None, time.monotonic()
                    last_tx, last_tx_change = None, time.monotonic()
                    continue
                crashed_since = 0.0
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

    def _heal_crashed_child(self, dead_since):
        """A player child that DIES — OOM kill, segfault — left 'playing'
        on the screen and silence in the room: the stall watchdog stood
        down on a dead child, and unlike a BT blip nothing auto-resumed
        (review 2026-07-18 R5). Respawn a CRASHED child: nonzero rc only
        (a deliberate stop clears self.child before this can see it, and
        a finished queue exits 0), and only when the persisted intent
        says audio was audibly playing (player.py's published pause
        state for this very target), the output can make sound, and the
        crash is fresh — the same no-surprise-audio window as a BT blip
        (BT_RESUME_S; an output that's away retries inside that window,
        then never again). Max 2 respawns per boot: a player that keeps
        dying has a real problem, and the bookmarked ghost state (press
        play to resume exactly there) is the honest fallback.

        Called from the watchdog's dead-child branch with the previous
        first-seen-dead stamp; returns the next stamp (0.0 = nothing to
        watch / respawned)."""
        with self.lock:
            child, target, source = self.child, self.target, self.source
            reverse, resume = self.reverse, self.resume
        if child is None or child.poll() in (None, 0):
            return 0.0  # no child, still alive, or a clean exit
        now = time.monotonic()
        dead_since = dead_since or now
        if now - dead_since > BT_RESUME_S or self._crash_respawns >= 2 \
                or source != "mpv" or not target:
            return dead_since
        try:  # persisted intent — never guess toward surprise audio
            with open(NOW_FILE) as f:
                published = json.load(f)
        except (OSError, ValueError):
            return dead_since
        if published.get("target") != target or published.get("paused"):
            return dead_since
        if not _audio_ready():
            return dead_since  # speaker away — retry within the window
        with self.lock:
            if self.child is not child or self._mpv_alive():
                return 0.0  # another path already spawned/stopped it
            self._crash_respawns += 1
            log(f"player died (rc {child.poll()}) while playing — "
                f"respawning ({self._crash_respawns}/2 this boot)")
            self.child = None
            self._spawn(target, reverse=reverse, resume=resume)
        return 0.0

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
        if is_spotify(target) or target.startswith(("http://", "https://")):
            _radio.touch_busy()  # network-heavy start: blind BT pages yield
            _PS_KICK.set()       # and wifi power save flips off NOW
        self.child = subprocess.Popen(args)
        self.child_started = time.monotonic()

    def play(self, target, fresh=False, episode=None, reverse=False,
             cache=None, resume=True, boot=False):
        # The backend probe/start (systemctl is-active, and up to 30s of
        # systemctl start against a parked unit) must never run under
        # ORCH.lock: every /status reader — the screen's 1/s poll —
        # queued behind it (review 2026-07-18 R2). Probing BEFORE the
        # lock is equivalent: a parked unit can't satisfy the
        # resume-in-place shortcut anyway (its API is down), and when
        # the shortcut does hit, the extra is-active probe is a no-op.
        backend_ok = not is_spotify(target) or self._ensure_spotify_backend()
        with self.lock:
            if boot:
                # The boot-resume thread is the LAST of three possible
                # starters: the A-press replay (command rule 4) and the
                # transport-up blip resume both spawn under this same
                # lock and stamp child_started. If anyone beat us here,
                # our job is done — proceeding would hit play()'s
                # stop-and-respawn shortcuts against a child whose IPC/
                # session isn't up yet and audibly restart it (triple-
                # start race, architect review 2026-07-18).
                if self.child_started > 0:
                    log("boot resume: playback already started — standing "
                        "down")
                    return {"status": "already-started"}
                try:
                    if spotify_playing(go_status(timeout=2)):
                        log("boot resume: spotify already playing — "
                            "standing down")
                        return {"status": "already-started"}
                except OSError:
                    pass  # api busy/down — the guards above suffice
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
            if is_spotify(target) and not backend_ok:
                # parked and genuinely offline: say so NOW — spawning a
                # player that waits 30s for a session that cannot come
                # just looks like a dead box (field report)
                log("play: no internet — spotify can't start")
                return {"source": "spotify", "target": target,
                        "error": "no-internet"}
            if target != self.target:
                # switching to a DIFFERENT context: flush the outgoing
                # spotify position first. The bookmarker thread isn't torn
                # down on a switch (unlike player.py on the mpv side) — it
                # just moves to the new target and drops the old bm_pending,
                # so the last <=30s of the previous url (incl. a seek just
                # made) would die with the throttle. Same gap the reboot
                # flush closes, triggered by a switch instead of a TERM.
                _flush_spotify_bookmark()
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
            if device == "bt" and _bt_transport_ready():
                with self.lock:
                    if self._mpv_alive():
                        try:
                            mpv_ipc(["set_property", "audio-device",
                                     f"alsa/{pcm}"])
                            log("output bt: deferred mpv switch applied")
                        except OSError:
                            pass
                # v0.0.7: reopen the output live (session kept, no
                # restart, no radio burst). Falls back to the config
                # rewrite + restart on a pre-v0.0.7 binary. Runs OUTSIDE
                # the lock either way — a restart queues every /status
                # reader behind it (review 2026-07-18 R2).
                if reopen_go_output(pcm):
                    log("output bt: deferred go-librespot output "
                        "reopened live")
                elif _retarget_go_librespot(pcm):
                    _note_go_restart()
                    log("output bt: deferred go-librespot retarget "
                        "applied")
            return {"unchanged": True, "output": device}
        with self.lock:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(OUT_FILE + ".tmp", "w") as f:
                json.dump({"output": device, "pcm": pcm}, f)
            os.replace(OUT_FILE + ".tmp", OUT_FILE)
            # BT quiet marker: the USER explicitly choosing the built-in
            # speaker (not btwatchd's drop fallback) tells btwatchd to stop
            # blind reconnect pages; ANY transition to bt clears it so a later
            # drop still recovers. (A drop -> fallback=True local -> marker
            # untouched -> btwatchd keeps its full reconnect ladder.)
            try:
                if device == "bt":
                    os.remove(_bt.BT_QUIET_FILE)
                elif device == "local" and not fallback:
                    open(_bt.BT_QUIET_FILE, "a").close()
            except OSError:
                pass
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
            # A resume IN FLIGHT loads its track PAUSED (play_spotify
            # loads, seeks, then unpauses) — so 'was playing' misses
            # it, the restart killed the loading session, and nobody
            # picked the baton back up: the player child waited 20s on
            # a dead session, resumed into an EMPTY new one (silent
            # no-op) and exited (field 2026-07-18 18:01:36 — box came
            # up mute). A live spotify player child IS playback intent.
            # Snapshot it (and the replay coordinates) under the lock;
            # the slow go-librespot surgery below runs WITHOUT it.
            spot_resuming = (self.child is not None
                             and self.child.poll() is None
                             and self.source == "spotify")
            resume_target, resume_flag = self.target, self.resume
        # From here on: status probe + systemctl restart, seconds of I/O
        # that used to hold ORCH.lock and froze every /status reader —
        # the screen's 1/s poll — for the whole switch (review R2).
        restarted = False
        go_action = "unchanged"
        if device == "bt" and not _bt_transport_ready():
            # same rule as mpv above: don't bounce go-librespot into a
            # device with no transport — the restart's wifi burst lands
            # exactly during AVDTP setup on the SHARED radio (the
            # coexistence load that crashes the Zero's BT firmware).
            # (A live reopen onto bluealsa can block on a mid-reconnect
            # speaker too, so it waits for the transport just the same.)
            pass
        elif reopen_go_output(pcm):
            # v0.0.7 live reopen: the audio output moves to the new
            # device WITHOUT tearing down the session — track, position,
            # volume and paused-state all survive, so there is nothing to
            # resume and no restart to dedup, and the shared radio stays
            # quiet. This is the path on a current binary.
            go_action = "reopened live"
        else:
            # pre-v0.0.7 fallback: audio_device is startup config there,
            # so the switch is a config rewrite + restart that kills the
            # session mid-song — we bring the music back from the
            # bookmark below.
            try:
                st = go_status(timeout=2)
            except OSError:
                st = {}  # api busy/flapping — the checks below cope
            # box-initiated playback only: a phone streaming its own
            # music through the box must not get hijacked into the
            # box's old target after the restart
            spot_was_playing = (spotify_playing(st)
                                and st.get("play_origin")
                                in ("go-librespot", "", None))
            restarted = _retarget_go_librespot(pcm)
            if restarted:
                _note_go_restart()
                go_action = "restarted"
            if restarted and (spot_was_playing or spot_resuming) \
                    and resume_target and is_spotify(resume_target):
                # unlike mpv (live IPC retarget), the restart killed
                # the session mid-song — bring the music back where
                # it was (player.py waits for the session, then
                # resumes from the bookmark). --exact: this is an
                # interruption, not a re-tap — even 0:08 into a song
                # must come back at 0:08, or it reads as a restart.
                # Stop a still-waiting old player first: left alive it
                # would fire ITS resume into the fresh session later.
                with self.lock:
                    if self.target == resume_target:
                        # (unless a fresh tap changed the target while
                        # the lock was down — that play owns the child)
                        self._stop_child()
                        self._spawn(resume_target, resume=resume_flag,
                                    exact=True)
                        log("output switch: resuming spotify from the "
                            "bookmark")
        log(f"output -> {device} (pcm {pcm}, "
            f"mpv {'switched' if mpv_switched else 'n/a'}, "
            f"go-librespot {go_action})")
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
            # nothing to flush at shutdown, and don't resurrect the just-
            # cleared bookmark if a reboot lands before the next tick
            _SPOT_PENDING_BM[0] = None
            _SPOT_LAST_PLAYING[0] = False
            log("stop (bookmark cleared)")
            return {"stopped": True}

    def _spot_control(self, action):
        """Run one spotify control; False = it timed out / failed. The
        timeout moment is remembered: for the next SPOT_TIMEOUT_HOLD_S a
        session that reads 'empty' is treated as still-loading, because
        the timed-out command is very likely still executing inside
        go-librespot (field 2026-07-18 16:14: the timed-out /next
        finished 14s later). Also turns what used to be a 500 on the
        HTTP handler into a clean busy-drop."""
        try:
            if action in ("next", "prev"):
                _radio.touch_busy()  # a skip = an imminent CDN track load
                _PS_KICK.set()       # wifi power save off before the fetch
            spotify_command(action)
            return True
        except OSError as e:
            self._spot_cmd_timeout_at = time.monotonic()
            # NOT 'press again': the timed-out command usually still
            # executes inside go-librespot (field 2026-07-18 20:26: the
            # 'dropped' /next landed 15s later) — a repeat press would
            # double-skip
            log(f"{action}: spotify control slow ({e.__class__.__name__})"
                " — it likely still lands; give it a moment")
            return False

    def command(self, action):
        # Drop, don't queue, presses that arrive while a control is still
        # running. go-librespot's API can take seconds per next/prev while
        # it loads the new track; each queued press then held this lock
        # for ANOTHER slow HTTP round, the UI timed out, the kid mashed
        # harder, and stale prev/next commands fired half a minute late
        # (field 2026-07-18 15:43: a prev storm landing out of order). A
        # dropped press is honest: nothing happened, press again.
        if not self.lock.acquire(timeout=1.0):
            log(f"{action}: control busy — dropped (previous command "
                "still running)")
            return {"routed": None, "busy": True}
        try:
            return self._command_locked(action)
        finally:
            self.lock.release()
            # a control may have moved the position (prev rewinds to 0,
            # next/seek jump) — wake the bookmarker so the in-memory
            # bookmark is fresh within a beat, not up to a 5s tick later
            _bm_wake.set()

    def _command_locked(self, action):
        _kick_bt_connect()  # any transport control = sound intent
        # 1) a running mpv session owns the controls
        if self._mpv_alive() and self.source == "mpv":
            try:
                if action == "prev":
                    # >5s into the episode: restart it (standard player
                    # semantics). A second prev (within 5s of the start)
                    # goes to the PREVIOUS episode.
                    pos = mpv_get("playback-time")
                    if isinstance(pos, (int, float)) and pos > 5:
                        cmd = ["seek", 0, "absolute"]
                    else:
                        # Resume ROTATES the queue so the bookmarked
                        # episode sits in slot 0 — the previous episode
                        # wraps to the END of the playlist. mpv's
                        # playlist-prev is a no-op at slot 0, which
                        # made the second prev fall through to
                        # 'nothing to control' (field 2026-07-18:
                        # 'prev just restarts the same track').
                        ppos = mpv_get("playlist-pos")
                        count = mpv_get("playlist-count")
                        if ppos == 0 and isinstance(count, int) \
                                and count > 1:
                            cmd = ["set_property", "playlist-pos",
                                   count - 1]
                        else:
                            cmd = ["playlist-prev"]
                elif action == "next":
                    # Symmetric with prev's wrap above: at the LAST slot
                    # playlist-next is a no-op, so 'next' got stuck and
                    # fell through to 'nothing to control' — with the
                    # queue rotated so slot 0 holds the resumed episode,
                    # the kid could never reach it by pressing next (only
                    # prev or a natural playout wrapped around). Field
                    # 2026-07-20, the 3-episode NRK series 'ninas-
                    # hemmelige-reise': next stuck on ep 2, only prev
                    # reached ep 3. Wrap to the first slot instead.
                    ppos = mpv_get("playlist-pos")
                    count = mpv_get("playlist-count")
                    if isinstance(ppos, int) and isinstance(count, int) \
                            and count > 1 and ppos >= count - 1:
                        cmd = ["set_property", "playlist-pos", 0]
                    else:
                        cmd = ["playlist-next"]
                else:
                    cmd = ["cycle", "pause"]  # playpause
                # A live mpv session OWNS the transport: a non-success
                # (end of queue, a transient refusal) must NOT fall
                # through to the spotify-replay path and log the
                # misleading 'nothing to control' (which also risked
                # respawning the wrong source).
                res = mpv_ipc(cmd)
                if res.get("error") == "success":
                    log(f"{action} -> mpv")
                else:
                    log(f"{action} -> mpv (no-op: {res.get('error')})")
                return {"routed": "mpv"}
            except OSError:
                pass  # child starting up; fall through but don't respawn
        # ONE short status probe feeds rules 2+3. The old shape called
        # spotify_playing() (a 5s-timeout status) and then go_status()
        # (another 5s) back to back while holding the control lock — a
        # busy go-librespot turned every press into ~10s of lock time.
        # And CRUCIALLY: an unreachable-because-BUSY API must never be
        # mistaken for a dead session (field 2026-07-18 15:44: a /next
        # during a slow track load fell through to rule 4 and RESTARTED
        # the whole album from 0:00).
        st = None
        try:
            st = go_status(timeout=2)
        except OSError:
            if self.source == "spotify" and _go_unit_active():
                log(f"{action}: go-librespot is busy (api not answering) "
                    "— dropped, press again")
                return {"routed": None, "busy": True}
        # 2) Spotify actively playing (covers phone-initiated sessions)
        if st and spotify_playing(st):
            if not self._spot_control(action):
                return {"routed": None, "busy": True}
            self.source = "spotify"
            self._persist()
            log(f"{action} -> spotify (active)")
            return {"routed": "spotify"}
        # 3) last thing used was Spotify -> resume/skip there — but only
        # when a track is actually loaded. After a reboot go-librespot
        # is logged in with an EMPTY session; a playpause into that void
        # "succeeds" silently and the button feels dead. Fall through to
        # rule 4 instead: replay the target, which resumes exactly.
        if self.source == "spotify" and st is not None:
            if (st.get("track") or {}) and not st.get("stopped"):
                if not self._spot_control(action):
                    return {"routed": None, "busy": True}
                log(f"{action} -> spotify (last)")
                return {"routed": "spotify"}
            # The session READS empty — but a SLOW track load looks
            # exactly like this for a beat (field 2026-07-18 16:14: /next
            # timed out at :35, prev at :47 saw an 'empty' session, Del 4
            # finished loading at :49). Two guards before a skip may
            # treat emptiness as the album's end:
            if action != "playpause":
                if (time.monotonic() - self._spot_cmd_timeout_at
                        < SPOT_TIMEOUT_HOLD_S):
                    # a control timed out moments ago — it is very likely
                    # STILL EXECUTING; emptiness proves nothing
                    log(f"{action}: session reads empty right after a "
                        "slow control — dropped (likely still loading)")
                    return {"routed": None, "busy": True}
                # transient-empty guard: re-read after a beat; a mid-load
                # blip resolves, a finished album stays empty
                time.sleep(EMPTY_RECHECK_S)
                try:
                    st2 = go_status(timeout=2)
                except OSError:
                    log(f"{action}: session state unclear — dropped")
                    return {"routed": None, "busy": True}
                if (st2.get("track") or {}) and not st2.get("stopped"):
                    if not self._spot_control(action):
                        return {"routed": None, "busy": True}
                    log(f"{action} -> spotify (loaded during recheck)")
                    return {"routed": "spotify"}
            log("spotify session is empty — replaying last target")
        # 4) dead session + remembered target -> bring it back. Playpause
        # always may (unambiguous 'give me music'). next/prev may TOO —
        # but only when the emptiness is TRUSTWORTHY: the API answered
        # and said so twice (album ran off its end — next on the last
        # Coco track must wrap to the start, not go dead), or the source
        # isn't spotify at all (a finished podcast queue). What must
        # never replay on a skip is an UNREACHABLE spotify API (st is
        # None): busy-not-dead — replaying there restarted a playing
        # album from 0:00 (the 15:44 disaster).
        trusted = st is not None or self.source != "spotify"
        if (action == "playpause" or trusted) \
                and self.target and not self._mpv_alive():
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

    def _settle_position(self, live, now):
        """Hold the reported position steady at the resume bookmark while
        mpv is still seeking there. A freshly spawned mpv reports
        playback-time as it loads (0, 1, 2 ...) and only THEN seeks to
        the bookmark, so the raw value flaps 0:00 -> 0:53 on every start
        and every reconnect respawn. player.py publishes resume_pos;
        report it verbatim until the live position reaches it (the seek
        landed), then track live — bounded to the first
        POSITION_SETTLE_MAX_S after spawn so a target that can never be
        reached can't freeze the bar forever."""
        try:
            rp = float(now.get("resume_pos")) if now else 0.0
        except (TypeError, ValueError, AttributeError):
            return live
        if rp <= RESUME_MIN_S:
            return live  # fresh start (ramps from 0 anyway) — nothing to hold
        if time.monotonic() - self.child_started > POSITION_SETTLE_MAX_S:
            return live
        if live is None or live < rp - POSITION_SETTLE_TOL_S:
            return rp  # the seek has not landed yet — hold at the bookmark
        return live  # within tolerance: seek landed, track live from here

    def status(self):
        # A control can hold the lock for ~20s against a wedged
        # go-librespot api (prev = status + command + re-status, all
        # slow) — the screen's 1/s poll must NEVER queue behind that
        # (field 2026-07-18 23:xx: whole UI frozen). 0.5s, then fall
        # back to racy-but-atomic attribute reads: a momentarily stale
        # source/target beats a dead screen.
        if self.lock.acquire(timeout=0.5):
            try:
                mpv_alive = self._mpv_alive()
                target, source = self.target, self.source
            finally:
                self.lock.release()
        else:
            mpv_alive = self._mpv_alive()
            target, source = self.target, self.source
        out = {"source": source, "target": target, "playing": False,
               "title": None, "position": None, "duration": None,
               "artwork": None, "episode_id": None, "shuffle": False,
               "spotify_offline": bool(_SPOT_OFFLINE[0]),
               "output": current_output()["output"]}
        if mpv_alive and source == "mpv":
            # gated on source too: a lingering/starting mpv child while
            # the box plays spotify leaked mpv's media-title (a raw URL)
            # over the spotify card (field 2026-07-18 23:xx)
            out["shuffle"] = self.mpv_shuffle
            pause = mpv_get("pause")
            if pause is None and (time.monotonic() - self.child_started
                                  < MPV_START_GRACE_S):
                # mpv is spawned but its IPC socket isn't up yet (the ~1-3s
                # window right after a tap): trust the intent player.py
                # published BEFORE launching mpv, so the screen shows
                # 'playing' at once instead of a dead card for a few seconds
                try:
                    with open(NOW_FILE) as f:
                        pause = bool(json.load(f).get("paused"))
                except (OSError, ValueError):
                    pause = False  # a fresh tap means to play
            out["playing"] = pause is False
            out["title"] = mpv_get("media-title")
            out["position"] = mpv_get("playback-time")
            out["duration"] = mpv_get("duration")  # None = live stream
            now = None
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
            out["position"] = self._settle_position(out["position"], now)
        # short timeout: /status is polled ~1/s by the single-threaded
        # screen, and go-librespot is briefly unresponsive while it
        # restarts (output switch / transport rebuild) — the default 5s
        # here froze the whole UI for ~5s on a BT drop (field 2026-07-17)
        st = go_status(timeout=GO_STATUS_TIMEOUT)
        if st.get("track"):
            self._go_st_cache = (time.monotonic(), st)
        elif not st:
            at, cached = getattr(self, "_go_st_cache", (0.0, None))
            # 5s covers a transient timeout — but a COLD track load
            # (first-listen playlist, api blocked 8-15s) outlived it and
            # the card fell to 'Nothing playing' mid-skip (field
            # 2026-07-19). A fresh BUSY marker or a recent timed-out
            # control proves a load is in flight: keep the card for the
            # full load-hold window. Cached kid albums never hit this —
            # their loads come off the disk cache in <1s.
            hold = GO_ST_HOLD_S
            if _radio.busy() or (time.monotonic() - self._spot_cmd_timeout_at
                                 < SPOT_TIMEOUT_HOLD_S):
                hold = SPOT_TIMEOUT_HOLD_S
            if cached and time.monotonic() - at < hold:
                st = cached  # a load is in flight — hold the good card
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
        # Offline-proof cover for the screen: the live artwork above is a
        # remote URL (gfx.nrk.no episode art, or a Spotify track's
        # i.scdn.co album cover) that can't load with no net — after a
        # reboot the box resumes before wifi is up, and the card stayed
        # blank. The cached collection cover (synced shows' cover.jpg,
        # a playlist's pre-built mosaic) is on disk for both kinds; serve
        # it so the screen always shows SOMETHING and upgrades to the
        # live cover once the network fetch lands.
        if target:
            try:
                out["artwork_local"] = content.collection_image(target)
            except Exception:
                out["artwork_local"] = None
        out["bt_waiting"], out["bt_ready"], out["bt_lost"] = \
            _bt_wait_state(out["playing"])
        # Steady 'is the configured speaker connected' for the screen's
        # status icon. /status polls at 1-2s, so the icon tracks a
        # connect/drop as fast as the popup does — the /system field
        # (same value) refreshes only every 30s and lagged visibly
        # (field 2026-07-20). Present only when a speaker is configured,
        # so a built-in-only box shows no BT icon.
        try:
            with open(_bt.MAC_FILE) as f:
                _spk = f.read().strip()
        except OSError:
            _spk = ""
        if _spk and out["output"] == "bt":
            # Only probe the BT transport (a bluealsa-aplay fork / dbus
            # enumerate, ~1/s) when the speaker is the ACTIVE output. On the
            # built-in speaker it is pointless AND it hammers a wedged
            # controller's bluealsa when the speaker is off/crashed. Omitting
            # the field (not sending False) lets the BT icon keep its last
            # value via /system's 30s poll — the UI fold is key-guarded on
            # the field's presence, so this is a clean no-op there. The
            # bt_lost/bt_waiting resume path below is untouched (QA).
            out["bt_connected"] = _bt_transport_ready()
        if out["bt_lost"] or out["bt_waiting"]:
            # both speaker popups offer the same escape — A plays on the
            # built-in speaker instead — but only where one exists
            # (BT-only boxes get X to connect and nothing else)
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
    a parked unit stays parked — the supervisor owns starting it.
    One debounce gate for BOTH triggers (the /wifi/connect hook and the
    IP watchdog): skip when go-librespot was already restarted moments
    ago (retarget, unpark, the other trigger racing) — its sockets are
    already bound to the new address."""
    with _GO_REBUILD_LOCK:
        fresh = time.monotonic() - _GO_REBUILD["at"] < NET_HEAL_COOLDOWN_S
    if fresh:
        log("network changed — go-librespot restarted recently, skipping")
        return
    log("network changed — restarting go-librespot (stale connections)")
    try:
        subprocess.run(["systemctl", "try-restart", "go-librespot"],
                       timeout=30)
        _note_go_restart()
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart after net change failed: {e!r}")


_netmgmt.net_changed[0] = _net_changed
NET_HEAL_COOLDOWN_S = float(os.environ.get("TAPBOX_NET_HEAL_COOLDOWN", "60"))
NET_IP_POLL_S = float(os.environ.get("TAPBOX_NET_IP_POLL", "15"))


def _wlan_ip():
    """The current IPv4 source address for internet traffic — a pure
    kernel route lookup (UDP connect sends NO packet), so polling this
    is radio-free. None = no default route (offline)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routable
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _ip_watchdog():
    """Catch the network changes our /wifi/connect hook can't see:
    NM-initiated failover, a DHCP lease on a new net, iface bounce.
    Field 2026-07-18 23:21: the iPhone hotspot died, NM auto-fell back
    to the home AP, and go-librespot kept zombie TCPs bound to the OLD
    address for minutes ('did not receive last pong', put-state
    timeouts) — every API call wedged, the whole box degraded. Rules:
    heal only on a REAL address change (A->B, or A->gone->B); A->gone->A
    is a blip (same lease came back, sockets still valid); offline is
    the supervisor's business, not ours."""
    last = _wlan_ip()  # seed: boot is not a change
    while True:
        _tick(NET_IP_POLL_S)
        try:
            cur = _wlan_ip()
            if cur is None:
                continue  # offline — keep the baseline (blip tolerance)
            if last is None:
                last = cur  # booted offline: first address = baseline
                continue
            if cur != last:
                _net_changed()
                last = cur
        except Exception as e:
            log(f"ip watchdog error: {e!r}")


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
            # bt_ready feeds the screen's connection icon — present ONLY
            # when a speaker is configured (no key -> no icon), True when
            # its A2DP transport is live. The 40s post-boot confusion
            # (log 2026-07-20: wifi up, speaker off, nothing playing)
            # reads at a glance instead of only via the popup.
            try:
                with open(_bt.MAC_FILE) as f:
                    _mac = f.read().strip()
            except OSError:
                _mac = ""
            if _mac:
                st["bt_ready"] = _bt_transport_ready()
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
            with _library.LIB_LOCK:  # vs the sweeper's profile sync
                save_library(lib)
            log(f"library updated ({sum(len(s['entries']) for s in lib['sections'])} entries)")
            # Free the disk held by entries just removed (or flipped to 'no
            # offline'): only entries that still want offline copies keep them.
            try:
                keep = [e["target"] for s in lib["sections"] for e in s["entries"]
                        if e.get("cache")]
                gone = content.prune_cache(keep)
                _sysinfo.invalidate_dir_sizes()  # /system sizes are stale
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
                _library.acknowledge_new(target)  # played it -> clear its dot
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
                with _library.LIB_LOCK:  # load->mutate->save, one writer
                    lib = load_library()
                    sec = next((s for s in lib["sections"]
                                if s["id"] == sid), None)
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
                            self._send(400,
                                       {"error": "image must be 100B-3MB"})
                            return
                        os.makedirs(ART_DIR, exist_ok=True)
                        with open(path + ".tmp", "wb") as f:
                            f.write(raw)
                        os.replace(path + ".tmp", path)
                        sec["image"] = path
                    save_library(normalize_library(lib))
                log(f"section logo {'set' if data else 'removed'}: {sid}")
                self._send(200, lib)
            elif self.path == "/wifi/reconnect":
                # on-demand 'get the net back now' (offline-Spotify popup's
                # X). Quiesce A2DP — the NM scan shares the 2.4GHz radio —
                # then actively wait for a known network; on success clear
                # the offline flag and unpark go-librespot so the next
                # play works at once, without waiting on the supervisor.
                try:
                    secs = min(max(int(body.get("secs") or 30), 5), 60)
                except (TypeError, ValueError):
                    secs = 30
                resume = _bt_quiesce()
                r = wifi_reconnect(secs)
                _bt_resume(resume)
                if r and r.get("ok"):
                    _SPOT_OFFLINE[0] = False
                    threading.Thread(
                        target=lambda: subprocess.run(
                            ["systemctl", "start", "go-librespot"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=30),
                        daemon=True).start()
                self._send(409 if r is None else 200,
                           r or {"error": "wifi operation already in progress"})
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
                # a wifi scan sweeps all 13 channels off-frequency — as
                # A2DP-hostile as BT discovery, so it gets the same
                # quiesce. Only on the bt output: a scan can't hurt the
                # built-in speaker, and stopping local playback for it
                # would be an audible interruption for nothing.
                resume = (_bt_quiesce()
                          if current_output()["output"] == "bt" else False)
                r = wifi_scan()
                _bt_resume(resume)
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
                # 240s, not 90: connect can legitimately run a full
                # firmware recover() (two re-attach rounds + rfkill
                # power-cycle) — a 90s SIGKILL could land BETWEEN
                # rfkill block and unblock and leave the radio down
                # for good (review 2026-07-18 R6)
                r = bt_action([cmd, mac], timeout=240 if cmd == "use" else 30)
                if cmd == "use":
                    _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/bt/rename":
                mac = str(body.get("mac") or "")
                if not MAC_RE.match(mac):
                    self._send(400, {"error": "valid mac required"})
                    return
                # a custom name for the speaker (blank clears it), sanitized
                # before it reaches BlueZ / the screen; a plain property
                # write, no radio quiesce.
                name = _clean_bt_name(body.get("name"))
                r = bt_action(["rename", mac, name], timeout=20)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/stop":
                self._send(200, ORCH.stop())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never let one request kill the daemon
            log(f"error on {self.path}: {e!r}")
            self._send(500, {"error": str(e)})


def _clean_bt_name(raw):
    """Sanitize a user-supplied speaker name before it reaches BlueZ and
    the screen: drop control/non-printable chars, collapse to a single
    line, cap the length. Blank (after cleaning) clears the alias."""
    return "".join(c for c in str(raw or "") if c.isprintable())[:64].strip()


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


# Box-initiated Spotify: the bookmarker keeps this true/false from a status
# fetched while go-librespot is still alive, so shutdown's was_playing snapshot
# can trust it WITHOUT a live query. At poweroff systemd TERMs go-librespot in
# the same cgroup, so a fresh status() there races its death and reads 'not
# playing' — mpv sidesteps the same race via its now-playing.json fallback, and
# Spotify had none. Box-initiated ONLY (source==spotify AND a spotify target),
# so a phone-driven Connect session never arms boot-resume.
_SPOT_LAST_PLAYING = [False]

# The freshest box-initiated bookmark, kept in memory so a reboot/poweroff
# can flush it even mid-song. The bookmarker throttles DISK writes (SD
# hygiene: 30s / on track change), so a position — e.g. a seek made seconds
# ago — otherwise lives only in bm_pending and dies with the thread at TERM,
# leaving boot-resume to continue from a stale spot. _on_term flushes this.
_SPOT_PENDING_BM = [None]


def _spotify_bookmarker():
    """Spotify's cloud remembers positions for ITS clients only — so we
    bookkeep like we do for mpv: while Spotify plays, snapshot the track,
    position and (when the box started it) the context every few seconds.
    play {uri, skip_to_uri} + seek replays it exactly, queue intact.
    The per-tick accept rules (box-initiated only, per-context files) live
    in spotify.bookmark_step/save_bookmark."""
    interval = 5
    # SD hygiene twin of player.py's throttle (energy audit 2026-07-20
    # #2): 5s ticks used to json+rename every tick — 720 SD bursts/hour
    # of listening. Write on track change or every 30s; when the tick
    # stops yielding a bookmark (pause/stop/phone takeover) the last
    # throttled position flushes immediately — pausing still bookmarks
    # the pause point, and only a hard power cut can lose <=30s.
    bm_flush_s = float(os.environ.get("TAPBOX_BOOKMARK_FLUSH", "30"))
    bm_written = [0.0, None]  # wall clock of last write, track uri
    bm_pending = None
    while True:
        woke = _bm_wake.wait(interval)
        _bm_wake.clear()
        try:
            st = go_status()
            # remember whether OUR spotify is audibly playing, for the
            # shutdown snapshot (see the _SPOT_LAST_PLAYING note) — reuses
            # this status, no extra I/O
            _SPOT_LAST_PLAYING[0] = (ORCH.source == "spotify"
                                     and bool(ORCH.target)
                                     and is_spotify(ORCH.target)
                                     and spotify_playing(st))
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
                _SPOT_PENDING_BM[0] = bm  # freshest position for shutdown flush
                if (bm.get("uri") != bm_written[1]
                        or time.monotonic() - bm_written[0] >= bm_flush_s):
                    _spotify.save_bookmark(bm)
                    bm_written = [time.monotonic(), bm.get("uri")]
                    bm_pending = None
                else:
                    bm_pending = bm
            elif bm_pending is not None:
                _spotify.save_bookmark(bm_pending)
                bm_written = [time.monotonic(), bm_pending.get("uri")]
                bm_pending = None
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
_BT_WAIT_LOCK = threading.Lock()  # status threads + the watcher tick
# A speaker reconnect can trigger several go-librespot restarts at once
# (btwatchd's output retarget + the blip-resume's output rebuild). Each
# restart bursts the shared radio mid-A2DP-setup, which makes the NEXT
# reconnect flap — a self-feeding storm (field log 2026-07-17 23:07). One
# restart per reconnect is enough: the rest just wait for the API.
_GO_REBUILD = {"at": 0.0}
_GO_REBUILD_LOCK = threading.Lock()
GO_REBUILD_COOLDOWN_S = float(os.environ.get("TAPBOX_GO_REBUILD_COOLDOWN", "8"))
BT_WAIT_TICK_S = float(os.environ.get("TAPBOX_BT_WAIT_TICK", "3"))
BT_WAIT_S = float(os.environ.get("TAPBOX_BT_WAIT_S", "180"))
# /status must stay snappy for the 1/s screen poll; go-librespot can hang
# a few seconds mid-restart, so cap how long its status query may block
GO_ST_HOLD_S = float(os.environ.get("TAPBOX_GO_ST_HOLD", "5"))
GO_STATUS_TIMEOUT = float(os.environ.get("TAPBOX_GO_STATUS_TIMEOUT", "1.5"))
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
    # _BT_WAIT writes go under _BT_WAIT_LOCK (a bare write can be
    # consumed by a stale transport-up event in _bt_wait_advance, review
    # R7) — but NEVER while holding ORCH.lock: the established order is
    # _BT_WAIT_LOCK -> ORCH.lock (_bt_wait_advance holds the wait lock
    # and calls source_is_spotify, which takes ORCH.lock). Taking them
    # in the opposite order here would be an AB/BA deadlock, so the
    # mpv branch stops the child under ORCH.lock and arms the wait
    # AFTER releasing it.
    stopped_mpv = False
    with ORCH.lock:
        if ORCH._mpv_alive():
            log("bt transport lost mid-play — stopping (bookmark survives)")
            ORCH._stop_child()
            stopped_mpv = True
    if stopped_mpv:
        with _BT_WAIT_LOCK:
            _BT_WAIT["lost"] = time.monotonic()
        return {"stopped": True}
    try:
        if spotify_playing():
            log("bt transport lost mid-play — pausing spotify")
            go("/player/pause")
            with _BT_WAIT_LOCK:
                _BT_WAIT["lost"] = time.monotonic()
                _BT_WAIT["lost_spotify"] = True
            return {"stopped": True}
    except OSError:
        pass  # go-librespot unreachable = nothing playing through it
    return {"stopped": False}


def _note_go_restart():
    """Record that go-librespot was just (re)started elsewhere (an output
    retarget), so a blip-resume rebuild on the same reconnect skips its
    own redundant restart."""
    with _GO_REBUILD_LOCK:
        _GO_REBUILD["at"] = time.monotonic()
    note_go_restart()  # cross-process marker: bt.py's route rewrite sees it


def _go_output_rebuild():
    """go-librespot's ALSA output dies WITH the bt transport
    ('snd_pcm_recover: No such device') and STAYS dead: a later
    /player/resume resumes the SESSION but never reopens the device —
    'playing' with no sound (field log 2026-07-17 19:21; two output
    toggles 'fixed' it only because the toggle restarts the service).
    Restart rebuilds the output; the session comes back empty, which
    routes any resume through the proven replay-last path. Wait for the
    login so a replay right after doesn't race the API.

    Deduped: if go-librespot was already (re)started in the last few
    seconds — the output retarget on the same reconnect, or a racing
    rebuild — its ALSA handle is already fresh, so we skip the restart
    and only wait for the API. Restarting again just re-bursts the
    shared radio and re-flaps the speaker (field storm 2026-07-17).

    Comes back on the CURRENT output device. Switching the output to bt
    while audio played on the built-in speaker leaves go-librespot's
    config on tapbox_local (the switch is deferred until the transport
    exists) — a plain restart would resume on the built-in one, so the
    kid pressed reconnect and it kept playing there, needing a manual
    bt/local toggle to move to the headset (field 2026-07-17). Retarget
    rewrites the config to the current output AND restarts, which is
    exactly what that toggle did."""
    with _GO_REBUILD_LOCK:
        now = time.monotonic()
        # 'fresh' also honours bt.py's ALSA-route restart (cross-process
        # marker): a first-pair connect writes the route + restarts, so
        # rebuilding the device again on the same transport-up is the
        # redundant second bounce we're deduping (only ever skips when
        # the retarget below finds nothing to change).
        fresh = (now - _GO_REBUILD["at"] < GO_REBUILD_COOLDOWN_S
                 or go_restarted_within(GO_REBUILD_COOLDOWN_S))
        _GO_REBUILD["at"] = now
    pcm = current_output().get("pcm")
    if pcm and reopen_go_output(pcm):
        # v0.0.7: reopen the dead ALSA handle LIVE on the current output.
        # This rebuilds the device WITHOUT restarting — the session stays
        # up, so audio flows again on its own with no replay-last and no
        # radio burst, and it also rewrites the config to the current
        # output (fixing a deferred switch left on the wrong device, the
        # exact job the retarget-restart used to do). Session intact means
        # login never dropped, so skip the wait-for-login below.
        log("go-librespot output reopened live on the current device "
            "(no restart, session kept)")
        return
    # pre-v0.0.7 fallback: audio_device is startup config, so rebuilding
    # the device means a restart (which drops the session -> replay-last).
    if pcm and _retarget_go_librespot(pcm):
        # config pointed at the wrong device — moved it + restarted
        log("go-librespot retargeted to the current output (restart)")
    elif fresh:
        log("go-librespot already rebuilt this reconnect — waiting for "
            "login, not restarting again")
    else:
        log("rebuilding go-librespot's audio output (restart)")
        try:
            subprocess.run(["systemctl", "restart", "go-librespot"],
                           timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"go-librespot output rebuild failed: {e!r}")
            return
    for _ in range(20):
        try:
            if go_status(timeout=2).get("username"):
                break
        except OSError:
            pass
        _tick(1)


def _speaker_back(now, elapsed, spot):
    """The speaker's transport just came up while a play intent (waiting)
    or a mid-play drop (lost) was pending. Within the blip window: just
    resume — no 'press A' homework, the kid already expressed the intent
    (field preference 2026-07-17). Beyond it: fall back to the press-A
    flash so a speaker that reappears much later can't blast audio by
    surprise (rebuilding go-librespot first for a spotify session, so
    that A lands on a live output instead of a dead handle)."""
    if elapsed <= BT_RESUME_S:
        threading.Thread(target=_bt_blip_resume, daemon=True).start()
        return 0.0  # no flash — playback comes back on its own
    if spot:
        threading.Thread(target=_go_output_rebuild, daemon=True).start()
    return now + BT_READY_FLASH_S


def _bt_wait_advance():
    """The transport-ready-driven transitions (auto-resume / press-A
    flash) and expiry. Runs from /status AND, crucially, from a
    background tick (_bt_wait_watcher): the screen sleeps and STOPS
    polling /status to save battery, so if this only ran on a poll the
    blip auto-resume never fired until a button woke the screen (field
    2026-07-17: 'have to press once for it to start'). No-op unless a
    wait is pending, so it's cheap on the timer."""
    # LOCK DISCIPLINE (review R7): _BT_WAIT_LOCK is a LEAF lock — held
    # only for dict reads/writes, never across I/O and never while
    # taking ORCH.lock. Writers that already hold ORCH.lock (play ->
    # _kick_bt_connect) may then take it safely. The transport probe
    # (dbus) and source_is_spotify (ORCH.lock) run between two short
    # critical sections, with a re-check in the second.
    with _BT_WAIT_LOCK:
        now = time.monotonic()
        # expire stale intents first (the kid walked away with the speaker
        # still off): each has its own clock, so age them independently
        if _BT_WAIT["lost"] and now - _BT_WAIT["lost"] > BT_WAIT_S:
            _BT_WAIT["lost"] = 0.0
            _BT_WAIT.pop("lost_spotify", None)
        if _BT_WAIT["since"] and now - _BT_WAIT["since"] > BT_WAIT_S:
            _BT_WAIT["since"] = 0.0  # stale intent
        pending = bool(_BT_WAIT["lost"] or _BT_WAIT["since"])
    if not pending or not _bt_transport_ready():
        return
    # The speaker coming back is ONE physical event. Both a mid-play
    # drop (lost) and a play-intent (since) can be pending together —
    # you switch the output to bt, then the link blips before A2DP
    # settles. Resuming for each separately calls _speaker_back TWICE
    # = two go-librespot restarts + two respawns racing (field storm
    # 2026-07-17 23:07, 'rebuilding output' logged twice a second
    # apart). Coalesce: resume ONCE, clearing both intents atomically.
    with _BT_WAIT_LOCK:
        if not (_BT_WAIT["lost"] or _BT_WAIT["since"]):
            return  # another thread consumed the event during the probe
        spot_flag = _BT_WAIT.pop("lost_spotify", False)
        # the most RECENT intent decides the blip window (be lenient:
        # a fresh play-intent right before the drop is still a blip)
        elapsed = now - max(_BT_WAIT["lost"], _BT_WAIT["since"])
        _BT_WAIT["lost"] = 0.0
        _BT_WAIT["since"] = 0.0
    ready_until = _speaker_back(now, elapsed,
                                spot_flag or source_is_spotify())
    with _BT_WAIT_LOCK:
        _BT_WAIT["ready_until"] = ready_until


def _bt_wait_watcher():
    """Drive the speaker-popup state off a timer, not just off /status —
    so a sleeping screen still gets the auto-resume the moment the
    speaker comes back."""
    while True:
        _tick(BT_WAIT_TICK_S)
        try:
            if _BT_WAIT["lost"] or _BT_WAIT["since"]:
                _bt_wait_advance()
        except Exception as e:
            log(f"bt wait watcher error: {e!r}")


def _bt_wait_state(playing):
    """(bt_waiting, bt_ready, bt_lost) for /status."""
    if playing:
        # Playback is on — normally every popup is done. Exception: the
        # user switched the output to the BT speaker but it isn't
        # connected, so audio is still coming from the built-in speaker.
        # Keep the 'not connected' popup up (X connects it, A drops back
        # to the built-in) until it connects or the output goes local.
        with _BT_WAIT_LOCK:
            since = _BT_WAIT["since"]
        if (since and current_output()["output"] == "bt"
                and not _bt_transport_ready()):
            return True, False, False
        with _BT_WAIT_LOCK:
            _BT_WAIT.update(lost=0.0, since=0.0, ready_until=0.0)
            _BT_WAIT.pop("lost_spotify", None)
        return False, False, False
    _bt_wait_advance()
    now = time.monotonic()
    with _BT_WAIT_LOCK:  # one consistent snapshot for the three flags
        return (bool(_BT_WAIT["since"]),  # pending, transport not ready
                now < _BT_WAIT["ready_until"],
                bool(_BT_WAIT["lost"]))


def source_is_spotify():
    with ORCH.lock:
        return ORCH.source == "spotify"


def _kick_bt_connect():
    """Play intent while the BT speaker has no transport: poke btwatchd
    to attempt a connect right away instead of waiting out its blind-retry
    backoff — up to 300s of silence after a boot where the speaker came
    on late. No-op on the built-in output or with the speaker connected."""
    if current_output()["output"] != "bt" or _bt_transport_ready():
        return
    with _BT_WAIT_LOCK:  # leaf lock — safe under ORCH.lock (see advance)
        _BT_WAIT["since"] = time.monotonic()  # screen shows "waiting..."
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
    """Actual-internet probe — shared with player/content via radio.py
    (env TAPBOX_PROBE_ADDR). Kept as a daemon-level name: tests patch
    daemon._internet_up."""
    return _radio.internet_up(2)


def _go_unit_active():
    """Is go-librespot's systemd unit running? Distinguishes 'API busy'
    (unit active, HTTP not answering — loading tracks, slow dealer) from
    'actually down'. Busy must never be treated as dead."""
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", "go-librespot"],
            timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired, AttributeError):
        return False


def _spotify_supervisor():
    """go-librespot is useless without internet, but restarts forever —
    each round costs ~1s of Zero CPU and journal noise. Park the unit
    while the box is offline; it is back within a minute of
    connectivity returning. Manual restarts while offline (e.g. an
    output switch rewrote its config) get re-parked on the next tick."""
    parked = False
    misses = 0
    park_grace_s = float(os.environ.get("TAPBOX_SPOT_PARK_GRACE", "180"))
    # Two cadences (review P1): while offline/parked the 20s tick keeps
    # recovery snappy (60s once made "no internet" lag a button-press
    # generation behind reality). While ONLINE and idle, each probe is a
    # radio wake out of the PS nap for nothing — noticing a loss can
    # wait 2 minutes (the play paths surface errors on their own).
    idle_tick_s = float(os.environ.get("TAPBOX_SPOT_PROBE_IDLE", "120"))
    while True:
        _tick(20 if (parked or _SPOT_OFFLINE[0]) else idle_tick_s)
        try:
            if _radio.paging():
                # a BT page owns the radio right now — a probe result is
                # noise either way (field 2026-07-18 20:17: page-deauthed
                # wifi + a probe mid-DHCP parked go-librespot for nothing)
                continue
            if _internet_up():
                misses = 0
                _SPOT_OFFLINE[0] = False
                if parked:
                    subprocess.run(["systemctl", "start", "go-librespot"],
                                   timeout=30)
                    log("spotify: internet is back — go-librespot started")
                    _note_go_restart()  # the ip watchdog must not re-restart it
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
                try:
                    if go_status(timeout=2).get("track"):
                        # A LOADED session — playing OR paused — is never
                        # parked. Playing audio is proof the net works
                        # (the probe lies under self-inflicted load: the
                        # cache sweep's downloads + the stream + A2DP all
                        # share the 2.4GHz radio). And parking a PAUSED
                        # session destroys the kid's pause: the session
                        # dies, the next button hits 'session is empty ->
                        # replaying last' and the music RESTARTS (field
                        # 2026-07-18 15:13-15:15: pause fought the parker
                        # for two minutes). Idle-shutdown covers the
                        # battery angle for a box left paused offline.
                        misses = 0
                        continue
                except OSError:
                    # Unreachable is NOT proof of dead: a BUSY api (rapid
                    # next/prev, slow track loads) times out too, and
                    # parking then kills live music (field 2026-07-18
                    # 15:44:38). Only park when the unit isn't even
                    # running; an active-but-slow go-librespot is left
                    # alone to finish what it's doing.
                    if _go_unit_active():
                        misses = 0
                        continue
                if _radio.uptime() < park_grace_s:
                    # boot is a storm of self-inflicted radio events (BT
                    # boot pages, wifi association, DHCP) — a failed
                    # probe here says nothing about the internet. Field
                    # 2026-07-18 20:17:11: parked 70s after boot because
                    # a page deauthed wifi mid-DHCP. Misses keep
                    # counting, so a REAL offline box parks the moment
                    # the grace expires.
                    continue
                _SPOT_OFFLINE[0] = True
                if _go_unit_active():  # don't fork systemctl every tick
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
            # box-initiated spotify: trust the state the bookmarker last saw
            # while go-librespot was alive (a fresh query here races its
            # cgroup TERM at poweroff); live probe stays as the fallback.
            playing = _SPOT_LAST_PLAYING[0] or spotify_playing()
        with open(LAST_FILE) as f:
            last = json.load(f)
        last["was_playing"] = bool(playing)
        with open(LAST_FILE + ".tmp", "w") as f:
            json.dump(last, f)
        os.replace(LAST_FILE + ".tmp", LAST_FILE)
    except Exception:
        pass


def _flush_spotify_bookmark():
    """Reboot/poweroff while OUR spotify plays: the bookmarker throttles
    disk writes, so the freshest position lives only in memory and would
    die with the thread at TERM — boot-resume then continues from a stale
    spot (field: seek to the start, reboot mid-song, resume lands back at
    the old position). Flush the last in-memory bookmark here. Playing-
    gated (stop/pause already flushed or cleared, and must not be
    resurrected), and it's an in-memory value — no live go_status(), which
    would race go-librespot's concurrent TERM (the daemon is deliberately
    NOT ordered after it)."""
    try:
        if _SPOT_LAST_PLAYING[0] and _SPOT_PENDING_BM[0] is not None:
            _spotify.save_bookmark(_SPOT_PENDING_BM[0])
    except Exception:
        pass


def _on_term(*_args):
    _flag_was_playing()
    _flush_spotify_bookmark()
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
    # Silent grace first (the speaker usually reconnects in 10-20s), then
    # — if the output is bt and the speaker is still away — raise the
    # bt_waiting popup via _kick_bt_connect and KEEP the resume armed for
    # its lifetime: transport-up auto-resumes (the blip machinery), A on
    # the popup plays on the built-in speaker. The old behavior died
    # SILENTLY at 90s — the kid saw a box that 'forgot' to continue
    # (field 2026-07-18 18:01, box came up mute).
    grace_at = time.monotonic() + BOOT_GRACE_S
    deadline = grace_at + BT_WAIT_S
    asked = False
    while time.monotonic() < deadline:
        if _audio_ready():
            break
        if not asked and time.monotonic() >= grace_at \
                and current_output()["output"] == "bt":
            _kick_bt_connect()  # arms the popup + pages the speaker once
            asked = True
            log("boot resume: speaker still away — asking on the screen "
                "(X: connect / A: box speaker)")
        time.sleep(BOOT_TICK_S)
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
            if _internet_up():  # through the test seam, unlike the old
                break           # inline socket copy (review M3)
            time.sleep(2)
    # Claim the transport-up event before playing: if the blip machinery
    # (armed by the popup's 'since') already consumed it and resumed,
    # play(boot=True) below stands down; clearing here makes sure it
    # can't ALSO fire after we start (one starter per event).
    with _BT_WAIT_LOCK:
        _BT_WAIT.update(lost=0.0, since=0.0, ready_until=0.0)
        _BT_WAIT.pop("lost_spotify", None)
    ORCH.play(target, reverse=bool(last.get("reverse")),
              resume=bool(last.get("resume", True)), boot=True)


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


def _a_cached_audio_file():
    """Newest downloaded episode, to warm the exact demux/decode/resample
    path the next play will fault in (newest = most likely to be tapped).
    None when the cache is empty."""
    cache = os.environ.get("TAPBOX_CACHE", "/var/lib/tapbox/cache")
    newest, newest_mtime = None, -1.0
    for root, _dirs, files in os.walk(cache):
        for fn in files:
            if fn.endswith((".mp3", ".m4a", ".aac", ".opus", ".ogg")):
                p = os.path.join(root, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if mt > newest_mtime:
                    newest, newest_mtime = p, mt
    return newest


def _prewarm_mpv():
    """The first mpv launch of a boot cold-loads mpv + the ffmpeg stack
    (tens of MB) from the SD card — field log 2026-07-17: 11s of silence
    before the first audio. A plain 'mpv --version' pages the binary in
    but NOT the demux/decode/resample path (dlopened on demand) nor the
    specific episode file — so the first real play still faulted them in.
    Instead decode ~0.3s of a cached episode to a NULL sink (no DAC
    touched, no sound, works with the speaker off): that warms the real
    codec + 44.1kHz resample path and the mp3's own pages. The delay keeps
    it off the boot rush — the point is a warm cache BEFORE the first play."""
    time.sleep(PREWARM_DELAY_S)
    warm = _a_cached_audio_file() or "av://lavfi:sine=f=440"
    try:
        subprocess.run(
            ["mpv", "--no-config", "--no-video", "--really-quiet",
             "--load-scripts=no", "--no-ytdl",
             "--ao=null", "--ao-null-untimed",  # decode full-speed, no DAC
             "--audio-samplerate=44100", "--audio-channels=stereo",
             "--length=0.3", warm],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        log("mpv prewarmed (decode path paged in)")
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"mpv prewarm failed: {e!r}")


WIFI_PS_TICK_S = float(os.environ.get("TAPBOX_WIFI_PS_TICK", "15"))
# Play intent kicks the governor NOW: waiting for the next tick left PS
# ON through an entire 27s resume (field 2026-07-18 20:04) — the flip
# must land before the CDN burst, not after it.
_PS_KICK = threading.Event()


def _streaming_now():
    """True while audio streams OVER THE NETWORK (Spotify, or mpv on a
    remote URL); False when idle/cached; None when it CANNOT be known
    right now. go-librespot's api blocks while it loads a track — which
    is precisely when the radio works hardest — so an unreachable api
    with a running unit means 'probably mid-load', NOT idle. The
    governor once read that blind spot as 'not streaming' and switched
    power save ON in the middle of a CDN download, stretching a track
    load to ~19s (field 2026-07-18 16:14:44)."""
    # A fresh BUSY marker = a network-heavy start/skip is in flight RIGHT
    # NOW, whatever the api says — during a /next the api can answer with
    # an idle-looking state mid-load, and the governor flipped PS ON in
    # the middle of the CDN fetch (field 2026-07-18 20:26:32, 23s skip)
    if _radio.busy():
        return True
    with ORCH.lock:
        alive = ORCH._mpv_alive()
    if alive and mpv_get("pause") is False:
        p = mpv_get("path")
        if isinstance(p, str) and p.startswith(("http://", "https://")):
            return True
    try:
        if spotify_playing():
            return True
    except OSError:
        if _go_unit_active():
            return None  # api busy (likely loading) — hold the PS state
    # a control that timed out moments ago means a load is very likely
    # still in flight even though the api now answers idle-ish — unknown,
    # never 'idle' (same field skip as above: the /next was 'dropped' at
    # 8s but executed at 23s)
    if time.monotonic() - ORCH._spot_cmd_timeout_at < SPOT_TIMEOUT_HOLD_S:
        return None
    return False


def _wifi_ps_governor():
    """Wi-Fi power save trades latency for battery — and the two sides
    win at DIFFERENT times. Idle/cached: PS on is pure battery win.
    Network streaming: the AP buffers packets until the radio's next
    nap-wakeup, and under BT coexistence those latency spikes starved
    go-librespot's control plane (put-state 'context deadline exceeded',
    /next timeouts — field 2026-07-18 15:30). Toggle PS off only while
    something streams over the net, back on when idle. Respects the
    boot-time choice: if PS was already OFF (perf mode / operator
    preference) the governor leaves it alone entirely."""
    if os.environ.get("TAPBOX_WIFI_PS_GOVERNOR", "1") != "1":
        return
    # Crash recovery FIRST: the marker means a previous daemon turned PS
    # off for a stream and died before turning it back on. Without it,
    # the baseline loop below reads 'off' for 5 minutes, stands down,
    # and PS stays off until the next reboot — +30-50mA around the clock
    # (energy audit 2026-07-20 #1). /run clears at boot, so an operator's
    # deliberate perf-mode PS-off (no marker) is still honored.
    if os.path.exists(_PS_OFF_MARKER):
        try:
            subprocess.run(["iw", "dev", "wlan0", "set", "power_save", "on"],
                           timeout=10, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            os.remove(_PS_OFF_MARKER)
            log("wifi ps governor: restored power save after restart "
                "(previous daemon died with PS off)")
            _ps_govern()  # marker proves PS is ours to manage — no baseline
            return
        except OSError:
            pass  # can't read/fix — fall through to the normal baseline
    # The baseline read must WAIT OUT the boot: at daemon start wlan0
    # exists but PS is still off — NetworkManager enables it ~2min later
    # and tapbox-power(save) re-asserts it. Reading 'off' once at t=0 and
    # standing down forever left PS ON through every stream (field
    # 2026-07-18 15:43: no governor log line the whole session, controls
    # starved). Poll until PS is seen ON once (then manage); a box whose
    # operator keeps PS off never shows 'on' and the governor stands down.
    managed = False
    tries = int(os.environ.get("TAPBOX_WIFI_PS_BASELINE_TRIES", "30"))
    for i in range(tries):
        try:
            r = subprocess.run(["iw", "dev", "wlan0", "get", "power_save"],
                               capture_output=True, text=True, timeout=10)
            if "on" in (r.stdout or ""):
                managed = True
                break
        except (OSError, subprocess.TimeoutExpired):
            pass  # no iw / wlan0 not up yet — keep waiting
        if i + 1 < tries:
            _tick(10)
    if not managed:
        log("wifi ps governor: power save never seen on — not managing")
        return
    _ps_govern()


_PS_OFF_MARKER = os.path.join(RUN_DIR, "tapbox-wifi-ps-off")


def _ps_mark(off):
    """Advisory crash-note: 'the governor set PS off'. Best-effort — a
    failed write only means a crash recovers PS on the next reboot
    instead of the next daemon start, i.e. exactly today's behavior."""
    try:
        if off:
            with open(_PS_OFF_MARKER, "w"):
                pass
        else:
            os.remove(_PS_OFF_MARKER)
    except OSError:
        pass


def _ps_govern():
    log("wifi ps governor: managing (ps off while streaming, on when idle)")
    ps_off = False  # current state we set (baseline = on)
    idle_since = None  # when continuous not-streaming began (PS still off)
    hyst_s = float(os.environ.get("TAPBOX_WIFI_PS_HYST", "180"))
    while True:
        _PS_KICK.wait(WIFI_PS_TICK_S)  # play intent ends the wait early
        _PS_KICK.clear()
        try:
            want_off = _streaming_now()
            if want_off is None:  # api mid-load: never flip PS blindly
                continue
            if want_off:
                idle_since = None
            elif ps_off:
                # Hysteresis: PS goes back ON only after a LONG idle.
                # Flipping 10s after a pause killed the Spotify AP TCP
                # (silent 'pong ack' death -> re-auth on the next play)
                # and a flip mid-activity has caused a field problem
                # every single time. ~3 min of PS-off idle costs ~0.2%
                # battery per pause (RF review 2026-07-18).
                if idle_since is None:
                    idle_since = time.monotonic()
                if time.monotonic() - idle_since < hyst_s:
                    continue
            if want_off == ps_off:
                continue
            subprocess.run(["iw", "dev", "wlan0", "set", "power_save",
                            "off" if want_off else "on"],
                           timeout=10, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            ps_off = want_off
            _ps_mark(want_off)
            log("wifi power save off (streaming)" if want_off
                else "wifi power save on (idle)")
        except Exception as e:
            log(f"wifi ps governor error: {e!r}")


def _audible_now():
    """Is anything actually making sound (or about to)? The cache
    sweeper's busy-gate: its downloads must never share the radio with
    live audio. A just-spawned mpv (IPC not up yet) counts as audible —
    that's exactly the tap->audio window the sweep must stay out of."""
    with ORCH.lock:
        alive = ORCH._mpv_alive()
        started = ORCH.child_started
    if alive:
        p = mpv_get("pause")
        if p is False:
            return True
        if p is None and time.monotonic() - started < MPV_START_GRACE_S:
            return True
    try:
        return bool(spotify_playing())
    except OSError:
        # api blocked + unit running = very likely mid-track-load, the
        # worst moment for sweep downloads to grab the radio — busy.
        # A parked/dead unit is genuinely not audible.
        return _go_unit_active()


def main():
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except ValueError:
        pass  # not the main thread (tests run main() in a thread)
    _library.BUSY_CHECK = _audible_now  # the sweep yields to live audio
    threading.Thread(target=_wifi_ps_governor, daemon=True).start()
    threading.Thread(target=_boot_resume, daemon=True).start()
    threading.Thread(target=_prewarm_mpv, daemon=True).start()
    threading.Thread(target=_bt_wait_watcher, daemon=True).start()
    threading.Thread(target=_cache_sweeper, daemon=True).start()
    threading.Thread(target=_spotify_bookmarker, daemon=True).start()
    # off the bind path (review 2026-07-18 B1): the re-enable forks
    # rfkill/iw/nmcli probes with up-to-5s timeouts, and running it
    # synchronously here delayed "listening" — the screen sits on its
    # splash waiting for /system until the server is up
    threading.Thread(target=_wifi_boot_reenable, daemon=True).start()
    threading.Thread(target=_wifi_watchdog, daemon=True).start()
    threading.Thread(target=_battery_runtime_tracker, daemon=True).start()
    threading.Thread(target=_spotify_supervisor, daemon=True).start()
    threading.Thread(target=_ip_watchdog, daemon=True).start()
    threading.Thread(target=_portal_server, daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log(f"listening on {BIND}:{PORT} (PWA: http://tapbox.local:{PORT})")
    server.serve_forever()


if __name__ == "__main__":
    main()
