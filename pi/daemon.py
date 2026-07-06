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
  GET  /output     current audio output ("bt" or "local")
  POST /output     {"device": "bt"|"local"} — mpv switches live over IPC;
                   go-librespot needs a config rewrite + service restart
  GET  /settings   box settings (screen timeout, idle shutdown, volume cap)
  PUT  /settings   update settings (validated; consumers re-read live)
  GET  /system     battery (PiSugar), disk/cache usage, wifi state, temps
  POST /system/wifi      {"enabled": bool} — rfkill wifi
  POST /system/shutdown  {"restart": bool} — graceful poweroff/reboot
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
from tapbox import content, mpv as _mpv, spotify as _spotify  # noqa: E402
from tapbox.paths import CACHE_DIR, SETTINGS_FILE, STATE_DIR  # noqa: E402

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
OUT_FILE = os.path.join(STATE_DIR, "output.json")
NOW_FILE = os.path.join(STATE_DIR, "now-playing.json")
LIB_FILE = os.environ.get("TAPBOX_LIBRARY", "/etc/tapbox/library.json")
GO_CONFIG = os.environ.get("TAPBOX_GO_CONFIG", "")  # go-librespot config.yml
PORT = int(os.environ.get("TAPBOX_PORT", "3679"))
# The parent PWA is served to the LAN (http://tapbox.local:3679). Keep this
# port firewalled from the internet — the API is deliberately auth-less on
# the home network (a PIN gate is a product-phase addition).
BIND = os.environ.get("TAPBOX_BIND", "0.0.0.0")
WEB_DIR = os.environ.get("TAPBOX_WEB") or (
    os.path.join(_here, "web") if os.path.isdir(os.path.join(_here, "web"))
    else "/usr/share/tapbox/web")
ORDERS = ("auto", "newest_first", "oldest_first")
OUTPUT_PCMS = {"bt": "tapbox_bt",
               "local": os.environ.get("TAPBOX_LOCAL_PCM", "tapbox_local")}


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def player_path():
    p = os.path.join(_here, "player.py")
    return p if os.path.exists(p) else "/usr/local/bin/tapbox-player"


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
            cache = e.get("cache", 0)
            if not isinstance(cache, int) or not 0 <= cache <= 100:
                raise ValueError("cache must be 0-100 (episodes to keep offline)")
            eid = str(e.get("id") or hashlib.sha1(target.encode()).hexdigest()[:8])
            if eid in seen:
                raise ValueError(f"duplicate entry id {eid}")
            seen.add(eid)
            sec["entries"].append(
                {"id": eid, "name": ename, "target": target, "order": order,
                 "cache": cache})
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


# --- background episode caching (the "offline: keep newest N" setting) ----------
# Entries with cache > 0 are synced by the daemon itself: right after the
# library is saved (add a podcast -> download starts immediately) and then
# every SYNC_INTERVAL so new episodes land without anyone pressing play.
# Syncs are incremental (existing files skipped, catalog cache TTL'd), run
# sequentially at nice 19, and failures are just retried next sweep.

SYNC_INTERVAL_S = int(os.environ.get("TAPBOX_SYNC_INTERVAL", 6 * 3600))
SYNC_DELAY_S = int(os.environ.get("TAPBOX_SYNC_DELAY", 30))
_sync_wake = threading.Event()


def _sync_args_for(target, n):
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        return ["sync", m.group(1), str(n), "podcast"]
    m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
    if m:
        return ["sync", m.group(1), str(n), "series"]
    if target.startswith(("http://", "https://")) and not is_spotify(target):
        return ["sync-feed", target, str(n)]
    return None  # spotify (global cache) / local folders (already offline)


def _cache_sweeper():
    time.sleep(SYNC_DELAY_S)  # let wifi come up after boot
    while True:
        for s in load_library()["sections"]:
            for e in s["entries"]:
                n = e.get("cache") or 0
                args = _sync_args_for(e["target"], n) if n > 0 else None
                if not args:
                    continue
                log(f"cache sweep: {e['name']} ({' '.join(args)})")
                try:
                    subprocess.run(
                        [sys.executable, content.__file__, *args],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=3600,
                        preexec_fn=lambda: os.nice(19))
                except (OSError, subprocess.TimeoutExpired) as exc:
                    log(f"cache sweep failed for {e['name']}: {exc!r}")
        _sync_wake.wait(SYNC_INTERVAL_S)
        _sync_wake.clear()


# --- expansion (entry -> playable, titled episode list) -------------------------

def _natural_order(target):
    """The order content.expand_entries returns for this kind of target.
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
                "order": "auto", "image": None, "episodes": []}
    entries = content.expand_entries(target)
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
                         "image": e.get("image"), "cached": bool(cached)})
    try:  # show-level artwork (local cover file when synced -> works offline)
        image = content.collection_image(target)
    except Exception:
        image = None
    return {"kind": "list", "name": name, "target": target, "order": order,
            "image": image, "episodes": episodes}


# --- settings (screen timeout, idle shutdown, volume cap) -----------------------

# Defaults double as the validation table: (default, min, max). 0 disables
# the screen timeout / idle shutdown.
SETTING_SPECS = {
    "screen_timeout_s": (30, 0, 600),
    "idle_shutdown_min": (30, 0, 240),
    "volume_cap": (100, 30, 100),
    "spotify_cache_gb": (20, 1, 100),
}


def load_settings():
    out = {k: spec[0] for k, spec in SETTING_SPECS.items()}
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        for k, spec in SETTING_SPECS.items():
            if isinstance(saved.get(k), (int, float)):
                out[k] = max(spec[1], min(spec[2], int(saved[k])))
    except (OSError, ValueError):
        pass
    return out


def update_settings(changes):
    if not isinstance(changes, dict):
        raise ValueError("settings must be an object")
    for k, v in changes.items():
        if k not in SETTING_SPECS:
            raise ValueError(f"unknown setting {k!r}")
        if not isinstance(v, (int, float)):
            raise ValueError(f"{k} must be a number")
        lo, hi = SETTING_SPECS[k][1], SETTING_SPECS[k][2]
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be {lo}-{hi}")
    merged = {**load_settings(), **{k: int(v) for k, v in changes.items()}}
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE + ".tmp", "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)
    log(f"settings updated: {changes}")
    if "spotify_cache_gb" in changes:
        _resize_spotify_cache(merged["spotify_cache_gb"])
    return merged


def _resize_spotify_cache(gb):
    """Write the size limit into go-librespot's config (startup-only there,
    like audio_device) and restart it. Eviction prunes on next start."""
    if not GO_CONFIG:
        return
    try:
        with open(GO_CONFIG) as f:
            text = f.read()
    except OSError:
        return
    new, n = re.subn(r"(?m)^(\s*size_limit:).*$", rf"\g<1> {gb}GB", text, count=1)
    if n == 0 or new == text:
        return
    with open(GO_CONFIG + ".tmp", "w") as f:
        f.write(new)
    os.replace(GO_CONFIG + ".tmp", GO_CONFIG)
    log(f"spotify cache limit -> {gb}GB (restarting go-librespot)")
    try:
        subprocess.run(["systemctl", "restart", "go-librespot"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart failed ({e!r}) — restart it manually")


# --- system status (battery, disk, wifi) ----------------------------------------

def pisugar_get(prop):
    """Query pisugar-server's TCP API, e.g. pisugar_get('battery') -> '84.2'."""
    try:
        with socket.create_connection(("127.0.0.1", 8423), timeout=2) as s:
            s.sendall(f"get {prop}\n".encode())
            s.settimeout(2)
            data = b""
            while b"\n" not in data:  # reply format: "battery: 84.2\n"
                chunk = s.recv(256)
                if not chunk:
                    break
                data += chunk
        text = data.decode(errors="ignore")
        return text.split(":", 1)[1].strip() if ":" in text else None
    except (OSError, IndexError):
        return None


def _safe_pct(raw):
    """A JSON-safe battery percentage: pisugar can return nan/inf while
    the charger toggles, and json.dumps(nan) is invalid JSON."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if -1 <= v <= 200 else None  # nan/inf fail the compare


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _run_out(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def wifi_state():
    """(enabled, ssid, ip) — enabled means not rfkill-blocked."""
    out = _run_out(["rfkill", "list", "wifi"]).lower()
    enabled = "blocked: yes" not in out  # soft or hard block = off
    ssid = None
    for line in _run_out(["iw", "dev", "wlan0", "link"]).splitlines():
        if line.strip().startswith("SSID:"):
            ssid = line.split(":", 1)[1].strip()
    ip = (_run_out(["hostname", "-I"]).split() or [None])[0]
    return enabled, ssid, ip


def system_status():
    batt = pisugar_get("battery")
    plugged = pisugar_get("battery_power_plugged")
    disk = None
    try:
        import shutil
        du = shutil.disk_usage(CACHE_DIR if os.path.isdir(CACHE_DIR) else "/")
        disk = {"total": du.total, "free": du.free}
    except OSError:
        pass
    caches = {}
    for name, p in (("podcasts", CACHE_DIR),
                    ("spotify", "/var/lib/tapbox/spotify-cache")):
        if os.path.isdir(p):
            caches[name] = _dir_size(p)
    temp = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = round(int(f.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        pass
    enabled, ssid, ip = wifi_state()
    return {"battery": _safe_pct(batt),
            "plugged": plugged == "true",
            "disk": disk, "caches": caches, "cpu_temp": temp,
            "wifi": {"enabled": enabled, "ssid": ssid, "ip": ip},
            "hostname": socket.gethostname()}


def set_wifi(enabled):
    cmd = "unblock" if enabled else "block"
    try:
        subprocess.run(["rfkill", cmd, "wifi"], timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}
    log(f"wifi {cmd}ed")
    en, ssid, ip = wifi_state()
    return {"enabled": en, "ssid": ssid, "ip": ip}


# --- bluetooth (delegates to play.sh — the pairing logic with all the
# --- hard-won BlueZ workarounds lives there, in ONE place) -----------------------

BT_FILE = os.environ.get("TAPBOX_BT_FILE", "/etc/tapbox/bt-headset")
BT_LOCK = threading.Lock()  # one pairing/connect operation at a time
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def play_cli():
    p = os.path.join(_here, "play.sh")
    return os.environ.get("TAPBOX_PLAY") or (
        p if os.path.exists(p) else "/usr/local/bin/tapbox-play")


def bt_status():
    configured = None
    try:
        with open(BT_FILE) as f:
            configured = f.read().strip() or None
    except OSError:
        pass
    devices = {}
    for line in _run_out(["bluetoothctl", "devices"]).splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device":
            devices[parts[1]] = {"mac": parts[1], "name": parts[2],
                                 "paired": False, "connected": False}
    for filt, key in (("Paired", "paired"), ("Connected", "connected")):
        out = _run_out(["bluetoothctl", "devices", filt])
        if filt == "Paired" and ("Invalid" in out or "Unknown" in out):
            out = _run_out(["bluetoothctl", "paired-devices"])  # older bluez
        for line in out.splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 2 and parts[0] == "Device" and parts[1] in devices:
                devices[parts[1]][key] = True
    return {"configured": configured, "pairing": BT_LOCK.locked(),
            "devices": sorted(devices.values(),
                              key=lambda d: d["name"].lower())}


def bt_action(args, timeout):
    """Run a play.sh BT command; None = another operation is in flight."""
    if not BT_LOCK.acquire(blocking=False):
        return None
    try:
        r = subprocess.run(["bash", play_cli(), *args], capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout + "\n" + r.stderr).strip()
        tail = "\n".join(out.splitlines()[-6:])
        log(f"bt {' '.join(args)} -> exit {r.returncode}")
        result = {"ok": r.returncode == 0, "output": tail}
    except (OSError, subprocess.TimeoutExpired) as e:
        result = {"ok": False, "output": f"failed: {e}"}
    finally:
        BT_LOCK.release()
    return {**result, **bt_status()}


def bt_scan():
    """~20s discovery; devices in pairing mode show up here. None = busy."""
    if not BT_LOCK.acquire(blocking=False):
        return None
    try:
        r = subprocess.run(["bash", play_cli(), "scan-raw"],
                           capture_output=True, text=True, timeout=60)
        found = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and MAC_RE.match(parts[0]):
                found.append({"mac": parts[0], "name": parts[1],
                              "audio": len(parts) > 2 and parts[2] == "yes"})
        # audio devices first, then by name
        found.sort(key=lambda d: (not d["audio"], d["name"].lower()))
        log(f"bt scan -> {len(found)} device(s)")
        return {"ok": True, "found": found}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "found": [], "output": f"failed: {e}"}
    finally:
        BT_LOCK.release()


def shutdown(restart=False):
    """Answer the HTTP request first, then power off."""
    cmd = ["reboot"] if restart else ["poweroff"]
    log(f"{'restart' if restart else 'shutdown'} requested")
    threading.Timer(1.0, lambda: subprocess.run(cmd)).start()
    return {"ok": True, "action": "restart" if restart else "poweroff"}


# --- audio output (bt speaker vs built-in/HAT) ----------------------------------

def current_output():
    try:
        with open(OUT_FILE) as f:
            d = json.load(f)
        return {"output": d.get("output") or "bt",
                "pcm": d.get("pcm") or "tapbox_bt"}
    except (OSError, ValueError):
        return {"output": "bt", "pcm": "tapbox_bt"}


def _retarget_go_librespot(pcm):
    """Point go-librespot's audio_device at pcm. Unlike mpv, its audio
    device is startup config — a change means config rewrite + restart.
    Returns True when the config was changed."""
    if not GO_CONFIG:
        return False
    try:
        with open(GO_CONFIG) as f:
            text = f.read()
    except OSError:
        return False
    new, n = re.subn(r"(?m)^audio_device:.*$", f"audio_device: {pcm}", text)
    if n == 0:
        new = text.rstrip("\n") + f"\naudio_device: {pcm}\n"
    if new == text:
        return False
    with open(GO_CONFIG + ".tmp", "w") as f:
        f.write(new)
    os.replace(GO_CONFIG + ".tmp", GO_CONFIG)
    try:
        subprocess.run(["systemctl", "restart", "go-librespot"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart failed ({e!r}) — config updated, "
            "restart it manually")
    return True


# --- static files + artwork proxy (the PWA) --------------------------------------

def artwork_roots():
    """Directories the artwork proxy may serve from: the episode cache and
    any local folder that is a library target (their cover.jpg files)."""
    roots = [os.path.realpath(CACHE_DIR)]
    for s in load_library()["sections"]:
        for e in s["entries"]:
            if os.path.isdir(e["target"]):
                roots.append(os.path.realpath(e["target"]))
    return roots


def artwork_allowed(path):
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r + os.sep)
               for r in artwork_roots())


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
            return {"output": device, "pcm": pcm,
                    "mpv_switched": mpv_switched,
                    "spotify_restarted": restarted}

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
               "artwork": None, "episode_id": None,
               "output": current_output()["output"]}
        if mpv_alive:
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
            out["source"] = "spotify"
            out["title"] = track.get("name")
            out["duration"] = (track.get("duration") or 0) / 1000 or None
            out["position"] = (st.get("position") or 0) / 1000
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


def main():
    threading.Thread(target=_cache_sweeper, daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log(f"listening on {BIND}:{PORT} (PWA: http://tapbox.local:{PORT})")
    server.serve_forever()


if __name__ == "__main__":
    main()
