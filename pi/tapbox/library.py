"""Library services: the parent-curated link collection, expansion into
playable episode lists, background episode caching, and the artwork-proxy
allowlist. Extracted verbatim from daemon.py."""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

from tapbox import content
from tapbox.paths import CACHE_DIR
from tapbox.spotify import is_spotify

LIB_FILE = os.environ.get("TAPBOX_LIBRARY", "/etc/tapbox/library.json")
ORDERS = ("auto", "newest_first", "oldest_first")


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def state_key(target):
    """The resume-bookmark key for a target (same rule as player.py):
    the podcast slug for NRK links, else a hash of the target."""
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        return m.group(1)
    return hashlib.sha1(target.encode()).hexdigest()[:12]


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


def _on_battery():
    from tapbox.sysinfo import pisugar_get  # deferred: avoids an import cycle
    return pisugar_get("battery_power_plugged") == "false"


def _cache_sweeper():
    time.sleep(SYNC_DELAY_S)  # let wifi come up after boot
    deliberate = False  # True when a library save woke us (sync now)
    while True:
        if not deliberate and _on_battery():
            # scheduled sweeps can wait for the charger: downloading new
            # episodes is exactly the kind of background work that should
            # not spend battery (or hotspot data) on a trip
            log("cache sweep skipped — on battery (runs when charging)")
            deliberate = _sync_wake.wait(SYNC_INTERVAL_S)
            _sync_wake.clear()
            continue
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
        deliberate = _sync_wake.wait(SYNC_INTERVAL_S)
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


