#!/usr/bin/env python3
"""tapboxd — TapBox orchestration daemon: one authority for playback.

Owns the answer to "what is playing / what played last" and routes all
commands, so cards, buttons, the CLI and (later) the parent PWA behave
coherently instead of guessing at each other. HTTP API on 127.0.0.1:3679:

  POST /play       {"target": <any link/path>, "fresh": bool,
                    "episode": <id>}  episode = start the queue there
  POST /playpause  |  /pause  |  /next  |  /prev  |  /stop
  POST /volume     {"volume": 0-100} or {"delta": +/-n} — routes to the
                   active source (mpv softvol / go-librespot volume)
  GET  /volume     current volume of the active source (0-100)
  GET  /status     unified now-playing (source, title, position, ...)
  GET  /library    the parent-curated library (sections -> named links)
  PUT  /library    replace the library (validated, atomic write)
  GET  /expand?id=<entry>|target=<url>   entry -> playable episode list
                   with titles + cached flags (offline-aware menus)

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
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GO_API = "http://127.0.0.1:3678"
MPV_SOCK = os.environ.get("TAPBOX_MPV_SOCK", "/run/tapbox-mpv.sock")
STATE_DIR = os.environ.get("TAPBOX_STATE", "/var/lib/tapbox/state")
LAST_FILE = os.path.join(STATE_DIR, "last-play.json")
VOL_FILE = os.path.join(STATE_DIR, "volume.json")
LIB_FILE = os.environ.get("TAPBOX_LIBRARY", "/etc/tapbox/library.json")
CACHE_DIR = os.environ.get("TAPBOX_CACHE", "/var/lib/tapbox/cache")
PORT = int(os.environ.get("TAPBOX_PORT", "3679"))
ORDERS = ("auto", "newest_first", "oldest_first")


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def is_spotify(target):
    return (target.startswith("spotify:") or "open.spotify.com" in target
            or "spotify.link/" in target)


def player_path():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player.py")
    return p if os.path.exists(p) else "/usr/local/bin/tapbox-player"


# --- go-librespot (Spotify) ---------------------------------------------------

def go(path, timeout=5, body=None):
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(GO_API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def go_status():
    try:
        with urllib.request.urlopen(GO_API + "/status", timeout=5) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return {}


def spotify_playing(st=None):
    st = go_status() if st is None else st
    return bool(st.get("track")) and not st.get("paused") and not st.get("stopped")


def spotify_command(action):
    if action == "prev":
        # Spotify's prev only rewinds first; make one gesture reach the
        # actual previous track (same logic as buttons.py).
        before = (go_status().get("track") or {}).get("uri")
        go("/player/prev")
        time.sleep(0.4)
        after = go_status()
        same = (after.get("track") or {}).get("uri") == before
        if same and (after.get("position") or 0) < 2000:
            go("/player/prev")
    else:
        go({"playpause": "/player/playpause", "next": "/player/next"}[action])


# --- mpv (player.py's IPC socket) ---------------------------------------------

def mpv_ipc(command):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(2)
        s.connect(MPV_SOCK)
        s.sendall(json.dumps({"command": command}).encode() + b"\n")
        for line in s.recv(65536).split(b"\n"):
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "error" in msg:
                return msg
    return {}


def mpv_get(prop):
    try:
        r = mpv_ipc(["get_property", prop])
    except OSError:
        return None
    return r.get("data") if r.get("error") == "success" else None


# --- library (parent-curated named links) --------------------------------------

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def normalize_library(obj):
    """Validate and normalize a library document; raises ValueError.
    Fills in missing ids (stable: sha1 of target) so clients can reference
    entries without carrying URLs around."""
    if not isinstance(obj, dict) or not isinstance(obj.get("sections"), list):
        raise ValueError("library must be an object with a 'sections' list")
    out = {"version": 1, "sections": []}
    seen = set()
    for s in obj["sections"]:
        if not isinstance(s, dict):
            raise ValueError("section must be an object")
        name = str(s.get("name") or "").strip()
        if not name:
            raise ValueError("section needs a name")
        sec = {"id": str(s.get("id") or _slug(name)), "name": name, "entries": []}
        for e in s.get("entries") or []:
            if not isinstance(e, dict):
                raise ValueError("entry must be an object")
            target = str(e.get("target") or "").strip()
            ename = str(e.get("name") or "").strip()
            if not target or not ename:
                raise ValueError("entry needs a name and a target")
            order = e.get("order") or "auto"
            if order not in ORDERS:
                raise ValueError(f"order must be one of {ORDERS}")
            eid = str(e.get("id") or hashlib.sha1(target.encode()).hexdigest()[:8])
            if eid in seen:
                raise ValueError(f"duplicate entry id {eid}")
            seen.add(eid)
            sec["entries"].append(
                {"id": eid, "name": ename, "target": target, "order": order})
        out["sections"].append(sec)
    return out


def load_library():
    try:
        with open(LIB_FILE) as f:
            return normalize_library(json.load(f))
    except (OSError, ValueError):
        return {"version": 1, "sections": []}


def save_library(lib):
    os.makedirs(os.path.dirname(LIB_FILE), exist_ok=True)
    tmp = LIB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(lib, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIB_FILE)


def find_entry(lib, entry_id):
    for s in lib["sections"]:
        for e in s["entries"]:
            if e["id"] == entry_id:
                return e
    return None


# --- expansion (entry -> playable, titled episode list) -------------------------

def _nrk():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (here, "/usr/local/bin"):  # repo checkout first, then installed
        if p not in sys.path:
            sys.path.append(p)
    import nrk
    return nrk


def _natural_order(target):
    """The order nrk.expand_entries returns for this kind of target.
    Heuristic — used to decide whether an explicit order needs a reverse."""
    if re.match(r"https?://radio\.nrk\.no/podkast/", target, re.I):
        return "newest_first"
    if re.match(r"https?://radio\.nrk\.no/serie/", target, re.I):
        return "oldest_first"   # serial stories play from the beginning
    if os.path.isdir(target):
        return "oldest_first"   # sorted filenames, part 1 first
    return "newest_first"       # RSS convention


def _cached_stems():
    """Basenames (sans extension) of every downloaded episode in the cache."""
    stems = set()
    for _root, _dirs, files in os.walk(CACHE_DIR):
        for f in files:
            stems.add(os.path.splitext(f)[0])
    return stems


def expand_target(target, order="auto", name=None):
    if is_spotify(target):
        # Not expandable without the Web API: a leaf "play all" entry.
        return {"kind": "spotify", "name": name, "target": target,
                "order": "auto", "episodes": []}
    entries = _nrk().expand_entries(target)
    if order != "auto" and order != _natural_order(target):
        entries = list(reversed(entries))
    stems = _cached_stems()
    episodes = []
    for e in entries:
        url = e["url"]
        eid = e.get("id")
        cached = (not url.startswith("http") and os.path.exists(url)) or \
                 (eid is not None and os.path.splitext(str(eid))[0] in stems)
        episodes.append({"id": eid, "title": e.get("title"), "url": url,
                         "cached": bool(cached)})
    return {"kind": "list", "name": name, "target": target, "order": order,
            "episodes": episodes}


# --- the orchestrator ----------------------------------------------------------

class Orchestrator:
    def __init__(self):
        self.lock = threading.Lock()
        self.child = None
        self.target = None
        self.source = None
        self.reverse = False
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

    def _spawn(self, target, fresh=False, episode=None, reverse=False):
        args = [sys.executable, player_path()]
        if fresh:
            args.append("--fresh")
        if reverse:
            args.append("--reverse")
        if episode:
            args += ["--episode", episode]
        args.append(target)
        self.child = subprocess.Popen(args)
        self.child_started = time.monotonic()

    def play(self, target, fresh=False, episode=None, reverse=False):
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
            self._spawn(target, fresh, episode, reverse)
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
        with self.lock:
            if self._mpv_alive() and self.source == "mpv":
                try:
                    if absolute is None:
                        cur = mpv_get("volume")
                        absolute = (100 if cur is None else cur) + delta
                    v = max(0, min(100, round(absolute)))
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
            v = max(0, min(100, round(absolute)))
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
        out = {"source": source, "target": target,
               "playing": False, "title": None, "position": None}
        if mpv_alive:
            out["playing"] = mpv_get("pause") is False
            out["title"] = mpv_get("media-title")
            out["position"] = mpv_get("playback-time")
        st = go_status()
        track = st.get("track") or {}
        out["spotify"] = {"playing": spotify_playing(st),
                          "track": track.get("name") or None,
                          "artists": track.get("artist_names") or []}
        if not mpv_alive and out["spotify"]["playing"]:
            out["playing"] = True
            out["source"] = "spotify"
            out["title"] = track.get("name")
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

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/status":
            self._send(200, ORCH.status())
        elif url.path == "/volume":
            self._send(200, ORCH.get_volume())
        elif url.path == "/library":
            self._send(200, load_library())
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
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self):
        if self.path != "/library":
            self._send(404, {"error": "not found"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            lib = normalize_library(json.loads(self.rfile.read(n)))
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        save_library(lib)
        log(f"library updated ({sum(len(s['entries']) for s in lib['sections'])} entries)")
        self._send(200, lib)

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
                if not target and body.get("id"):
                    entry = find_entry(load_library(), body["id"])
                    if not entry:
                        self._send(404, {"error": f"no library entry {body['id']}"})
                        return
                    target = entry["target"]
                    # Play in the same order the menu showed the episodes
                    reverse = (entry["order"] != "auto"
                               and entry["order"] != _natural_order(target))
                if not target:
                    self._send(400, {"error": "target or id required"})
                    return
                self._send(200, ORCH.play(target, bool(body.get("fresh")),
                                          body.get("episode") or None, reverse))
            elif self.path in ("/playpause", "/next", "/prev"):
                self._send(200, ORCH.command(self.path[1:]))
            elif self.path == "/pause":
                self._send(200, ORCH.pause())
            elif self.path == "/volume":
                if body.get("volume") is None and body.get("delta") is None:
                    self._send(400, {"error": "volume or delta required"})
                    return
                self._send(200, ORCH.volume(absolute=body.get("volume"),
                                            delta=body.get("delta")))
            elif self.path == "/stop":
                self._send(200, ORCH.stop())
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never let one request kill the daemon
            log(f"error on {self.path}: {e!r}")
            self._send(500, {"error": str(e)})


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"listening on 127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
