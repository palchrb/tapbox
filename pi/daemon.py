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
  GET  /expand?id=<entry>|target=<url>   entry -> playable episode list
                   with titles + cached flags (offline-aware menus)
  GET  /output     current audio output ("bt" or "local")
  POST /output     {"device": "bt"|"local"} — mpv switches live over IPC;
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
  POST /wifi/hotspot  {"enabled": bool} — the setup hotspot (TapBox-<host>).
                      Also auto-starts on fresh boxes: no saved wifi network
                      and nothing connected. A :80 redirect server + wildcard
                      DNS (dnsmasq-shared.d) pops the phone's captive portal
                      straight into the PWA.
  GET  /bt         known/paired/connected speakers + the configured one
  POST /bt/scan    scan ~20s, list nearby devices (pick one -> /bt/connect)
  POST /bt/pair    {"name"?} — one-button flow: auto-pair the single audio
                   device in pairing mode (play.sh's validated flow)
  POST /bt/connect {"mac"}  — connect a speaker; pairs first when the mac
                   is new (picked from a scan), routes audio to it
  POST /bt/forget  {"mac"}  — drop the bond

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
from tapbox import mpv as _mpv, spotify as _spotify  # noqa: E402
from tapbox.paths import STATE_DIR  # noqa: E402

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
PORT = int(os.environ.get("TAPBOX_PORT", "3679"))
PORTAL_PORT = int(os.environ.get("TAPBOX_PORTAL_PORT", "80"))
# The parent PWA is served to the LAN (http://tapbox.local:3679). Keep this
# port firewalled from the internet — the API is deliberately auth-less on
# the home network (a PIN gate is a product-phase addition).
BIND = os.environ.get("TAPBOX_BIND", "0.0.0.0")
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

from tapbox import bt as _bt  # noqa: E402
from tapbox.library import (  # noqa: E402
    ORDERS, artwork_allowed, expand_target, find_entry, load_library,
    normalize_library, save_library, _cache_sweeper, _natural_order,
    _sync_wake)
from tapbox.netmgmt import (  # noqa: E402
    HOTSPOT_PSK, HOTSPOT_SSID, hotspot_active, set_wifi, start_hotspot,
    stop_hotspot, wifi_connect, wifi_forget, wifi_scan, _wifi_watchdog)
from tapbox.output import (  # noqa: E402
    OUTPUT_PCMS, OUT_FILE, current_output, _i2s_card_present,
    _retarget_go_librespot)
from tapbox.sysinfo import (  # noqa: E402
    load_settings, shutdown, system_status, update_settings)

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
        self.mpv_shuffle = False  # mpv has no queryable shuffle state
        try:
            with open(LAST_FILE) as f:
                d = json.load(f)
            self.target, self.source = d.get("target"), d.get("source")
            self.reverse = bool(d.get("reverse"))
            if self.target:
                log(f"remembered last play: [{self.source}] {self.target}")
        except (OSError, ValueError):
            pass
        self.child_started = 0.0
        threading.Thread(target=self._arbiter, daemon=True).start()

    def _arbiter(self):
        """The box stays Spotify Connect-discoverable while mpv plays; if the
        user picks it from the phone mid-podcast, both would fight over the
        BT output. Watch for that takeover and yield mpv gracefully (its
        bookmark is saved, so the card resumes later)."""
        while True:
            time.sleep(4)
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

    def _persist(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"target": self.target, "source": self.source,
                       "reverse": self.reverse, "updated": time.time()}, f)
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

    def _spawn(self, target, fresh=False, episode=None, reverse=False,
               cache=None):
        args = [sys.executable, player_path()]
        if fresh:
            args.append("--fresh")
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
             cache=None):
        with self.lock:
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
            self._stop_child()
            self._spawn(target, fresh, episode, reverse, cache)
            self.mpv_shuffle = False  # fresh queue plays in order
            self.target = target
            self.reverse = reverse
            self.source = "spotify" if is_spotify(target) else "mpv"
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

    def set_output(self, device):
        pcm = OUTPUT_PCMS.get(device)
        if not pcm:
            return None  # handler answers 400
        with self.lock:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(OUT_FILE + ".tmp", "w") as f:
                json.dump({"output": device, "pcm": pcm}, f)
            os.replace(OUT_FILE + ".tmp", OUT_FILE)
            mpv_switched = False
            if self._mpv_alive():
                try:  # mpv can retarget its audio device live
                    mpv_switched = mpv_ipc(
                        ["set_property", "audio-device", f"alsa/{pcm}"]
                    ).get("error") == "success"
                except OSError:
                    pass
            restarted = _retarget_go_librespot(pcm)
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
        with self.lock:
            self._stop_child()
            try:
                go("/player/pause")
            except OSError:
                pass
            log("stop")
            return {"stopped": True}

    def command(self, action):
        with self.lock:
            # 1) a running mpv session owns the controls
            if self._mpv_alive() and self.source == "mpv":
                cmds = {"playpause": ["cycle", "pause"],
                        "next": ["playlist-next"], "prev": ["playlist-prev"]}
                try:
                    if mpv_ipc(cmds[action]).get("error") == "success":
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
            # 3) last thing used was Spotify -> resume/skip there
            if self.source == "spotify":
                try:
                    spotify_command(action)
                    log(f"{action} -> spotify (last)")
                    return {"routed": "spotify"}
                except OSError:
                    pass
            # 4) dead session + remembered target -> bring it back (resumes)
            if self.target and not self._mpv_alive():
                self._spawn(self.target, reverse=self.reverse)
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
                if now.get("url") == mpv_get("path"):
                    out["episode_id"] = now.get("id")
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
        if not mpv_alive and track and not st.get("stopped"):
            out["playing"] = sp_playing
            out["shuffle"] = bool(st.get("shuffle_context"))
            out["source"] = "spotify"
            out["title"] = track.get("name")
            out["duration"] = (track.get("duration") or 0) / 1000 or None
            # position lives on the track object (ms, live-extrapolated)
            out["position"] = (track.get("position") or 0) / 1000
            out["artwork"] = out["spotify"]["artwork"]
        return out


ORCH = Orchestrator()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the journal clean
        pass

    def _send(self, code, obj):
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
            self._send(200, load_library())
        elif url.path == "/output":
            self._send(200, current_output())
        elif url.path == "/settings":
            self._send(200, load_settings())
        elif url.path == "/system":
            self._send(200, system_status())
        elif url.path == "/bt":
            self._send(200, bt_status())
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
                if not target:
                    self._send(400, {"error": "target or id required"})
                    return
                self._send(200, ORCH.play(target, bool(body.get("fresh")),
                                          body.get("episode") or None, reverse,
                                          cache))
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
                r = ORCH.set_output(body.get("device"))
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
            elif self.path in ("/wifi/connect", "/wifi/forget"):
                ssid = str(body.get("ssid") or "").strip()
                if not ssid or len(ssid) > 32:
                    self._send(400, {"error": "ssid required (max 32 chars)"})
                    return
                if self.path == "/wifi/connect":
                    r = wifi_connect(ssid, str(body["password"])
                                     if body.get("password") else None)
                else:
                    r = wifi_forget(ssid)
                self._send(409 if r is None else 200,
                           r or {"error": "wifi operation already in progress"})
            elif self.path == "/bt/scan":
                r = bt_scan()
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/bt/pair":
                args = ["connect"]
                if body.get("name"):
                    args.append(str(body["name"]))
                r = bt_action(args, timeout=120)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path in ("/bt/connect", "/bt/forget"):
                mac = str(body.get("mac") or "")
                if not MAC_RE.match(mac):
                    self._send(400, {"error": "valid mac required"})
                    return
                cmd = "use" if self.path == "/bt/connect" else "forget"
                r = bt_action([cmd, mac], timeout=90 if cmd == "use" else 30)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/stop":
                self._send(200, ORCH.stop())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never let one request kill the daemon
            log(f"error on {self.path}: {e!r}")
            self._send(500, {"error": str(e)})


def _audio_ready():
    """Is the active output able to make sound yet? BT speakers reconnect
    a little while after boot; don't start playback into a void."""
    if current_output()["output"] == "local":
        return _i2s_card_present()
    try:
        mac = open(_bt.MAC_FILE).read().strip()
    except OSError:
        return True  # no speaker configured — nothing to wait for
    if not mac:
        return True
    try:
        r = subprocess.run(["bluealsa-aplay", "-L"], capture_output=True,
                           text=True, timeout=10)
        return mac.lower() in r.stdout.lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _flag_was_playing():
    """At shutdown (SIGTERM from systemd), record whether something was
    audibly playing — boot resume only continues in that case, so a box
    that was OFF/paused never surprises anyone by blasting on power-on."""
    try:
        playing = False
        if ORCH.child is not None and ORCH.child.poll() is None:
            playing = mpv_get("pause") is False
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
    mpv content resumes at the exact second via the bookmark; a Spotify
    context restarts from its beginning (positional resume needs the Web
    API context — documented limitation)."""
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
    ORCH.play(target, reverse=bool(last.get("reverse")))


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


def main():
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except ValueError:
        pass  # not the main thread (tests run main() in a thread)
    threading.Thread(target=_boot_resume, daemon=True).start()
    threading.Thread(target=_cache_sweeper, daemon=True).start()
    threading.Thread(target=_wifi_watchdog, daemon=True).start()
    threading.Thread(target=_portal_server, daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log(f"listening on {BIND}:{PORT} (PWA: http://tapbox.local:{PORT})")
    server.serve_forever()


if __name__ == "__main__":
    main()
