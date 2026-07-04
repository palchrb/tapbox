#!/usr/bin/env python3
"""tapboxd — TapBox orchestration daemon: one authority for playback.

Owns the answer to "what is playing / what played last" and routes all
commands, so cards, buttons, the CLI and (later) the parent PWA behave
coherently instead of guessing at each other. HTTP API on 127.0.0.1:3679:

  POST /play       {"target": <any link/path>, "fresh": bool}
  POST /playpause  |  /next  |  /prev  |  /stop
  GET  /status     unified now-playing (source, title, position, ...)

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

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GO_API = "http://127.0.0.1:3678"
MPV_SOCK = os.environ.get("TAPBOX_MPV_SOCK", "/run/tapbox-mpv.sock")
STATE_DIR = os.environ.get("TAPBOX_STATE", "/var/lib/tapbox/state")
LAST_FILE = os.path.join(STATE_DIR, "last-play.json")
PORT = int(os.environ.get("TAPBOX_PORT", "3679"))


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def is_spotify(target):
    return (target.startswith("spotify:") or "open.spotify.com" in target
            or "spotify.link/" in target)


def player_path():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player.py")
    return p if os.path.exists(p) else "/usr/local/bin/tapbox-player"


# --- go-librespot (Spotify) ---------------------------------------------------

def go(path, timeout=5):
    req = urllib.request.Request(GO_API + path, data=b"{}",
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


# --- the orchestrator ----------------------------------------------------------

class Orchestrator:
    def __init__(self):
        self.lock = threading.Lock()
        self.child = None
        self.target = None
        self.source = None
        try:
            with open(LAST_FILE) as f:
                d = json.load(f)
            self.target, self.source = d.get("target"), d.get("source")
            if self.target:
                log(f"remembered last play: [{self.source}] {self.target}")
        except (OSError, ValueError):
            pass

    def _persist(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"target": self.target, "source": self.source,
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

    def _spawn(self, target, fresh=False):
        args = [sys.executable, player_path()]
        if fresh:
            args.append("--fresh")
        args.append(target)
        self.child = subprocess.Popen(args)

    def play(self, target, fresh=False):
        with self.lock:
            self._stop_child()
            self._spawn(target, fresh)
            self.target = target
            self.source = "spotify" if is_spotify(target) else "mpv"
            self._persist()
            log(f"play [{self.source}] {target}")
            return {"source": self.source, "target": target}

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
                self._spawn(self.target)
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
        if self.path == "/status":
            self._send(200, ORCH.status())
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
                if not target:
                    self._send(400, {"error": "target required"})
                    return
                self._send(200, ORCH.play(target, bool(body.get("fresh"))))
            elif self.path in ("/playpause", "/next", "/prev"):
                self._send(200, ORCH.command(self.path[1:]))
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
