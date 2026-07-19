"""Library services: the parent-curated link collection, expansion into
playable episode lists, background episode caching, and the artwork-proxy
allowlist. Extracted verbatim from daemon.py."""

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from tapbox import content, spotify, spotify_web
from tapbox.paths import ART_DIR, CACHE_DIR, STATE_DIR
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
        image = s.get("image")  # optional section logo (uploaded via PWA)
        if image:
            if not isinstance(image, str) or len(image) > 500:
                raise ValueError("section image must be a short string")
            sec["image"] = image
        user = s.get("spotify_user")  # section follows a Spotify profile:
        if user:                      # its entries are sweeper-managed
            if not isinstance(user, str) or not 0 < len(user.strip()) <= 100:
                raise ValueError("spotify_user must be a short string")
            sec["spotify_user"] = user.strip()
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
            # -1 = keep all episodes offline; 0 = none; 1..100 = newest N
            if not isinstance(cache, int) or not (cache == -1 or 0 <= cache <= 100):
                raise ValueError("cache must be -1 (all) or 0-100 (episodes "
                                 "to keep offline)")
            resume = e.get("resume", True)  # False = always play from the start
            if not isinstance(resume, bool):
                raise ValueError("resume must be true or false")
            eid = str(e.get("id") or hashlib.sha1(target.encode()).hexdigest()[:8])
            if eid in seen:
                raise ValueError(f"duplicate entry id {eid}")
            seen.add(eid)
            sec["entries"].append(
                {"id": eid, "name": ename, "target": target, "order": order,
                 "cache": cache, "resume": resume})
        out["sections"].append(sec)
    return out


# mtime-keyed parse cache: /status re-loads the library every second in
# the box's most common state (stopped-but-remembered), and the full
# json.load + normalize per poll is pure CPU on a battery box (review
# P5). Callers mutate the returned dict, so hand out a deepcopy — still
# ~10x cheaper than re-parsing, and mtime_ns catches every save.
_LIB_CACHE = {"key": None, "lib": None}


def load_library():
    try:
        st = os.stat(LIB_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"version": 1, "sections": []}
    if _LIB_CACHE["key"] != key:
        try:
            with open(LIB_FILE) as f:
                _LIB_CACHE["lib"] = normalize_library(json.load(f))
            _LIB_CACHE["key"] = key
        except (OSError, ValueError):
            return {"version": 1, "sections": []}
    return copy.deepcopy(_LIB_CACHE["lib"])


def library_with_covers():
    """load_library() + best-effort show artwork per entry (menu covers
    for the screen UI; the PWA ignores the field). Cheap by construction:
    collection_image only consults local files and in-process caches —
    never the network. Entries without a synced/remembered cover get
    None until their feed is first expanded or cached."""
    lib = load_library()
    for s in lib.get("sections", []):
        for e in s.get("entries", []):
            try:
                e["image"] = content.collection_image(e["target"])
            except Exception:
                e["image"] = None
    return lib


# Serializes every load->mutate->save of library.json: three writers
# exist (PUT /library, /library/section-logo, the sweeper's profile
# sync right after every save) and an unserialized pair silently
# reverts the loser's edit (review 2026-07-18 R4). Never hold this
# across network I/O — fetch first, then lock, re-load, apply, save.
LIB_LOCK = threading.Lock()


def save_library(lib):
    _EXPAND_CACHE.clear()  # order/cache settings may have changed
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


# --- profile-follow sections (spotify_user) --------------------------------------
# A section with a spotify_user is a subscription: its entries mirror that
# profile's PUBLIC playlists. The parent curates from their phone by making
# playlists public/private; the sweeper picks the change up here.

def sync_profile_sections():
    """Refresh every profile-follow section from the Web API. Returns True
    when the library changed (and was saved). A profile that can't be
    fetched (offline, no credentials, deleted user) keeps the entries from
    the last successful sweep — the box never loses content over a blip."""
    users = [s.get("spotify_user") for s in load_library()["sections"]
             if s.get("spotify_user")]
    if not users:
        return False
    # Network first, WITHOUT the lock — a slow Web API call must never
    # block a parent's PUT /library
    fetched = {}
    for user in users:
        try:
            fetched[user] = spotify_web.user_playlists(user)
        except Exception as exc:
            log(f"profile sync: {user}: {exc!r}")
    # Then re-load fresh under the lock and apply: edits that landed
    # while we were fetching are preserved instead of reverted
    with LIB_LOCK:
        lib = load_library()
        # Manually curated targets win: a playlist already in a normal
        # section is skipped here, or normalize_library would reject the
        # duplicate id.
        seen = {e["target"] for s in lib["sections"]
                if not s.get("spotify_user") for e in s["entries"]}
        changed = False
        for sec in lib["sections"]:
            user = sec.get("spotify_user")
            if not user:
                continue
            if user not in fetched:  # fetch failed — keep last sweep's list
                seen.update(e["target"] for e in sec["entries"])
                continue
            # Rebuild the list but PRESERVE per-entry settings the parent
            # set (cache/order/resume) for playlists that persist — the
            # old wholesale rebuild reset cache to 0 on every sync, which
            # silently disarmed spotify pre-caching on followed playlists.
            prev = {e["target"]: e for e in sec["entries"]}
            entries = [{"name": p["name"], "target": p["target"],
                        "order": prev.get(p["target"], {}).get("order",
                                                               "auto"),
                        "cache": prev.get(p["target"], {}).get("cache", 0),
                        "resume": prev.get(p["target"], {}).get("resume",
                                                                True)}
                       for p in fetched[user] if p["target"] not in seen]
            seen.update(e["target"] for e in entries)
            if [(e["name"], e["target"]) for e in entries] != \
               [(e["name"], e["target"]) for e in sec["entries"]]:
                sec["entries"] = entries
                changed = True
                log(f"profile sync: {user}: {len(entries)} public playlist(s)")
        if changed:
            save_library(normalize_library(lib))
        return changed


# --- background episode caching (the "offline: keep newest N" setting) ----------
# Entries with cache > 0 are synced by the daemon itself: right after the
# library is saved (add a podcast -> download starts immediately) and then
# every SYNC_INTERVAL so new episodes land without anyone pressing play.
# Syncs are incremental (existing files skipped, catalog cache TTL'd), run
# sequentially at nice 19, and failures are just retried next sweep.

SYNC_INTERVAL_S = int(os.environ.get("TAPBOX_SYNC_INTERVAL", 6 * 3600))
# 90s (not 30): the boot resume fires ~30s in, and the sweep's podcast
# downloads saturate the Zero's single 2.4GHz link — a track skip right
# after boot then waited on a starved go-librespot control call (field
# log 2026-07-17: /next timed out ~20s after resume, mid-sweep). Push the
# first sweep past the initial interaction window.
SYNC_DELAY_S = int(os.environ.get("TAPBOX_SYNC_DELAY", 90))
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


SWEEP_STAMP = os.path.join(STATE_DIR, "last-sweep.json")
SYNC_STAGGER_S = int(os.environ.get("TAPBOX_SYNC_STAGGER", 4))
# The sweep must NEVER disturb active listening: its downloads share the
# single 2.4GHz radio with the Spotify stream AND the A2DP link, and a
# saturated radio starves control calls and fools the offline prober
# (field 2026-07-18: the sweep made pausing a song a two-minute fight).
# The daemon sets BUSY_CHECK to "is anything audible right now"; while it
# returns True the sweep holds off, and an in-flight download is
# ABANDONED (terminated, retried next sweep — syncs are incremental so
# little is lost) the moment playback starts.
BUSY_CHECK = None  # set by the daemon; None = never busy (CLI use)
SYNC_BUSY_RECHECK_S = int(os.environ.get("TAPBOX_SYNC_BUSY_RECHECK", 30))


def _busy():
    try:
        return bool(BUSY_CHECK and BUSY_CHECK())
    except Exception:
        return False  # a broken check must never stall the sweep forever


class SweepYield(Exception):
    """Raised when a running sync is abandoned because playback started."""


def _last_sweep():
    """Wall-clock time of the last completed sweep, or 0. Wall-clock (not
    monotonic) so it survives reboots — the whole point is to NOT sweep
    again on every restart when the last one was within the interval."""
    try:
        with open(SWEEP_STAMP) as f:
            return float(json.load(f)["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _stamp_sweep():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SWEEP_STAMP + ".tmp", "w") as f:
            json.dump({"at": time.time()}, f)
        os.replace(SWEEP_STAMP + ".tmp", SWEEP_STAMP)
    except OSError:
        pass


def _sync_one(args):
    """Run one content.py sync, kept off the audio's back: nice-19, and
    pinned to a single core (taskset) so the ffmpeg transcode can't take
    both cores from playback. Downloads are sequential (one at a time).
    Watched: if playback starts MID-download the child is terminated and
    SweepYield raised — the radio belongs to the music, and the next
    sweep re-runs the (incremental) sync for pennies."""
    cmd = [sys.executable, content.__file__, *args]
    if _TASKSET:
        cmd = [_TASKSET, "-c", "1"] + cmd
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            preexec_fn=lambda: os.nice(19))
    deadline = time.monotonic() + 3600
    while True:
        try:
            proc.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        if _busy() or time.monotonic() > deadline:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(cmd, 3600)
            raise SweepYield()


_TASKSET = shutil.which("taskset")


def _cache_sweeper():
    # Sweeps run on battery too: a box that mostly lives off the charger
    # otherwise never syncs at all — no fresh episodes, no covers. The
    # downloads run nice-19 + one-core so playback always wins.
    # TTL across reboots: if the last sweep was within SYNC_INTERVAL_S,
    # wait out the remainder instead of re-sweeping seconds after every
    # boot (redundant network+CPU that stole a track skip — field log
    # 2026-07-17). Fresh boxes / long-off boxes still sweep at SYNC_DELAY.
    due_in = max(SYNC_DELAY_S,
                 _last_sweep() + SYNC_INTERVAL_S - time.time())
    _sync_wake.wait(due_in)
    _sync_wake.clear()
    while True:
        while _busy():  # active listening owns the radio — hold the sweep
            _sync_wake.wait(SYNC_BUSY_RECHECK_S)
            _sync_wake.clear()
        try:
            # First, so new playlists get covers in this very sweep.
            sync_profile_sections()
        except Exception as exc:
            log(f"profile sync failed: {exc!r}")
        lib = load_library()
        try:
            # Spotify covers (oEmbed): fetch what's missing, drop orphans.
            # Cheap once cached — one network round-trip per NEW entry.
            content.ensure_spotify_art(
                [e["target"] for s in lib["sections"] for e in s["entries"]])
        except Exception as exc:
            log(f"spotify art fetch failed: {exc!r}")
        try:
            content.shrink_covers()  # one-time downscale of old full-size art
        except Exception as exc:
            log(f"cover shrink failed: {exc!r}")
        for s in lib["sections"]:
            for e in s["entries"]:
                n = e.get("cache") or 0
                # n>0 keep newest N, n==-1 keep all, n==0 no offline copies
                if n != 0 and is_spotify(e["target"]):
                    # Spotify pre-cache (fork v0.0.3): POST /cache/download
                    # pulls the whole context into go-librespot's disk
                    # cache without playing — every later skip is a cache
                    # hit instead of a cold CDN load. Async + internally
                    # rate-limited (concurrency/delay/jitter + circuit
                    # breaker), skips already-cached tracks, so a repeat
                    # request per sweep is cheap. Same discipline as the
                    # podcast syncs: only fires when nothing is audible.
                    while _busy():
                        _sync_wake.wait(SYNC_BUSY_RECHECK_S)
                        _sync_wake.clear()
                    try:
                        uri = spotify.to_uri(e["target"])
                        if uri:
                            spotify.go("/cache/download", timeout=10,
                                       body={"uri": uri})
                            log(f"cache sweep: {e['name']} (spotify "
                                "pre-cache queued)")
                    except OSError as exc:
                        log(f"spotify pre-cache {e['name']}: {exc!r}")
                    continue
                args = _sync_args_for(e["target"], n) if n != 0 else None
                if not args:
                    continue
                while _busy():  # re-checked before EVERY entry
                    _sync_wake.wait(SYNC_BUSY_RECHECK_S)
                    _sync_wake.clear()
                log(f"cache sweep: {e['name']} ({' '.join(args)})")
                try:
                    _sync_one(args)
                except SweepYield:
                    log(f"cache sweep yields to playback ({e['name']} "
                        "abandoned — retried next sweep)")
                except (OSError, subprocess.TimeoutExpired) as exc:
                    log(f"cache sweep failed for {e['name']}: {exc!r}")
                _sync_wake.wait(SYNC_STAGGER_S)  # breathe between entries
                _sync_wake.clear()
        _stamp_sweep()
        _sync_wake.wait(SYNC_INTERVAL_S)  # a library save wakes us early
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


_EXPAND_CACHE = {}  # (target, order, name) -> (monotonic, result)
EXPAND_TTL_S = 300  # menus re-open constantly; feeds change hourly at most


def expand_target(target, order="auto", name=None):
    if is_spotify(target):
        # Not expandable without the Web API: a leaf "play all" entry.
        # The cover (oEmbed, fetched by the sweeper) still shows.
        try:
            image = content.collection_image(target)
        except Exception:
            image = None
        return {"kind": "spotify", "name": name, "target": target,
                "order": "auto", "image": image, "episodes": []}
    key = (target, order, name)
    hit = _EXPAND_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < EXPAND_TTL_S:
        return hit[1]
    result = _expand_target_uncached(target, order, name)
    _EXPAND_CACHE[key] = (time.monotonic(), result)
    return result


def _expand_target_uncached(target, order, name):
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
    """Directories the artwork proxy may serve from: the episode cache,
    uploaded section logos, and any local folder that is a library target
    (their cover.jpg files)."""
    roots = [os.path.realpath(CACHE_DIR), os.path.realpath(ART_DIR)]
    for s in load_library()["sections"]:
        for e in s["entries"]:
            if os.path.isdir(e["target"]):
                roots.append(os.path.realpath(e["target"]))
    return roots


def artwork_allowed(path):
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r + os.sep)
               for r in artwork_roots())


