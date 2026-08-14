"""NRK/RSS/folder link expansion + episode cache (vibb.content, ex nrk.py).

Ported from palchrb's rfid_sonos_backend app.py, minus the Sonos-specific
parts (x-sonos-http URIs, DIDL metadata). The strategy is the same:

- radio.nrk.no/serie/<slug>: whole radio series via the psapi series
  catalog (cached/incremental like podcasts), episode 1 first — these are
  serial stories, so chronological order. Streams are HLS (no download).
- radio.nrk.no/serie/<serie>/<programId>: walk psapi metadata _links.next
  to queue the series from that episode onwards, resolving each episode's
  stream URL via the psapi playback manifest.
- radio.nrk.no/podkast/<slug>/<episodeId>: resolve the episode's mp3 via
  the psapi playback manifest (RSS enclosure match as fallback).
- radio.nrk.no/podkast/<slug>: play the whole catalog, newest episode
  first. NOTE: the official RSS at podkast.nrk.no is often truncated to
  the last few episodes, so the full episode list is fetched from the
  psapi radio catalog (the same source nrk-pod-feeds uses) and each
  episode's stream URL resolved from its playback manifest, in parallel.
- Any URL ending in .rss/.xml (e.g. an nrk-pod-feeds mirror): play all
  enclosures.

Feeds are fetched on demand, but two caches make repeated taps cheap and
enable offline playback (this is the spec's "auto-cache podcasts" story):

- Catalog cache: the resolved episode list per podcast is stored in
  CACHE_DIR with a 12h TTL, so a re-tap within that window makes zero
  psapi calls. A stale catalog is served when the network is down.
- Episode cache: `python3 nrk.py sync <slug> [count]` downloads the
  newest episodes (default 50) to CACHE_DIR/<slug>/; expand() returns
  local file paths for cached episodes and stream URLs for the rest.
  rfid.py/play.sh kick off a background sync whenever a podcast plays.

Unknown URLs pass through untouched (mpv + yt-dlp handles them).
"""

import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor


def _log(msg):
    print(f"nrk: {msg}", file=sys.stderr, flush=True)

PSAPI = "https://psapi.nrk.no"
MAX_EPISODES = 100
try:
    from vibb.paths import CACHE_DIR
except ImportError:  # run as a script (sync subprocess)
    CACHE_DIR = os.environ.get("VIBB_CACHE", "/var/lib/vibb/cache")
CATALOG_TTL_S = 12 * 3600

# The PLAYBACK path sets this (player.py): use whatever catalog/feed
# listing is cached, however old — pressing play must never wait on
# psapi/feed refreshes (or their offline timeouts). Refreshes belong to
# the background sync and the menu's /expand.
STALE_OK = False
SYNC_COUNT = 50


def _online():
    """Quick connectivity probe (mirrors player.online), kept local so
    content.py has no import cycle with player.py. VIBB_OFFLINE=1 forces
    offline (travel switch / tests). Plain IP:port — no DNS to hang on.

    Browsing is offline-first: when this returns False, expand serves the
    cached listing straight away instead of blocking on a doomed fetch that
    only times out (8s+) and falls back to the same cache anyway."""
    if os.environ.get("VIBB_OFFLINE"):
        return False
    # same VIBB_PROBE_ADDR as radio.internet_up, inlined to keep this
    # file runnable as a standalone stdlib-only script (sync subprocess)
    host, _, port = os.environ.get("VIBB_PROBE_ADDR",
                                   "1.1.1.1:443").rpartition(":")
    try:
        socket.create_connection((host, int(port)), timeout=2).close()
        return True
    except (OSError, ValueError):
        return False


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "vibb/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url):
    return json.loads(_get(url))


_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def _parse_feed(feed_bytes):
    """(channel_image, [(url, title, image)]) for an RSS feed."""
    root = ET.fromstring(feed_bytes)
    chan = root.find("./channel")
    chan_img = None
    if chan is not None:
        it = chan.find(f"{_ITUNES}image")
        if it is not None and it.get("href"):
            chan_img = it.get("href")
        else:
            classic = chan.find("./image/url")
            if classic is not None and classic.text:
                chan_img = classic.text.strip()
    items = []
    for item in root.findall("./channel/item"):
        enc = item.find("enclosure")
        if enc is None or "url" not in enc.attrib:
            continue
        t = item.find("title")
        title = t.text.strip() if t is not None and t.text else None
        it = item.find(f"{_ITUNES}image")
        img = it.get("href") if it is not None and it.get("href") else None
        items.append((enc.attrib["url"], title, img))
    return chan_img, items


def _pick_image(images, want=300):
    """A single URL from a psapi image list [{url, width}, ...]: the
    smallest variant that is at least `want` px wide (240x240 screen +
    PWA thumbnails — no reason to pull the 960px one)."""
    if not isinstance(images, list):
        return None
    cands = sorted((im for im in images
                    if isinstance(im, dict) and im.get("url")),
                   key=lambda im: im.get("width") or 0)
    for im in cands:  # smallest variant that is big enough
        if (im.get("width") or 0) >= want:
            return im["url"]
    return cands[-1]["url"] if cands else None


def _manifest_url(episode_id, kind="podcast"):
    # podcast episodes resolve via manifest/podcast, series episodes via
    # manifest/program (same pattern as palchrb's app.py used)
    mtype = "podcast" if kind == "podcast" else "program"
    try:
        m = _get_json(f"{PSAPI}/playback/manifest/{mtype}/{episode_id}")
        assets = (m.get("playable") or {}).get("assets") or []
        if assets and assets[0].get("url"):
            return assets[0]["url"]
    except OSError:
        pass
    return None


def _episode_stub(ep):
    self_href = ((ep.get("_links") or {}).get("self") or {}).get("href", "")
    eid = self_href.rstrip("/").split("/")[-1]
    title = (ep.get("titles") or {}).get("title") or ""
    image = _pick_image(ep.get("squareImage") or ep.get("image"))
    return {"id": eid, "title": title, "image": image} if eid else None


def _series_image(slug, kind="podcast"):
    """The show-level artwork from the catalog root (series.squareImage)."""
    try:
        d = _get_json(f"{PSAPI}/radio/catalog/{kind}/{slug}")
        s = d.get("series") or {}
        return _pick_image(s.get("squareImage") or s.get("image"))
    except (OSError, ValueError):
        return None


def _psapi_episodes(slug, kind="podcast"):
    """Full [{id, title}] list from the psapi catalog, oldest first."""
    stubs = []
    href = f"/radio/catalog/{kind}/{slug}/episodes?page=1&pageSize=50&sort=asc"
    while href and len(stubs) < MAX_EPISODES:
        data = _get_json(PSAPI + href)
        for ep in (data.get("_embedded") or {}).get("episodes") or []:
            stub = _episode_stub(ep)
            if stub:
                stubs.append(stub)
        href = ((data.get("_links") or {}).get("next") or {}).get("href")
    return stubs[:MAX_EPISODES]


def _episode_file(slug, episode_id, kind="podcast"):
    # podcasts are plain mp3 downloads; series are HLS captured to m4a
    ext = "mp3" if kind == "podcast" else "m4a"
    return os.path.join(CACHE_DIR, slug, f"{episode_id}.{ext}")


def _new_episodes(slug, known_ids, kind="podcast"):
    """[{id, title}] newer than anything we know, oldest first. Walks pages
    newest-first and stops at the first known id: steady-state cost is ONE call."""
    new = []
    href = f"/radio/catalog/{kind}/{slug}/episodes?page=1&pageSize=50&sort=desc"
    while href and len(new) < MAX_EPISODES:
        data = _get_json(PSAPI + href)
        for ep in (data.get("_embedded") or {}).get("episodes") or []:
            stub = _episode_stub(ep)
            if stub and stub["id"] in known_ids:
                return list(reversed(new))
            if stub:
                new.append(stub)
        href = ((data.get("_links") or {}).get("next") or {}).get("href")
    return list(reversed(new))


def _resolve_manifests(stubs, kind="podcast"):
    with ThreadPoolExecutor(max_workers=8) as ex:
        urls = list(ex.map(lambda s: _manifest_url(s["id"], kind), stubs))
    resolved = [{**s, "url": u} for s, u in zip(stubs, urls) if u]
    if len(resolved) < len(stubs):
        _log(f"warning: {len(stubs) - len(resolved)} episode(s) had no playable manifest")
    return resolved


def _catalog(slug, kind="podcast"):
    """[{id, title, url}] oldest first. Cached with a TTL. Refreshes are
    incremental: already-resolved episodes are reused (NRK's CDN URLs are
    stable), so one new episode costs 1 catalog call + 1 manifest call — not
    a full re-walk. A stale catalog beats nothing when the network is down."""
    prefix = "catalog" if kind == "podcast" else f"catalog-{kind}"
    path = os.path.join(CACHE_DIR, f"{prefix}-{slug}.json")
    cached = None
    try:
        with open(path) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        pass
    # Old cache formats lack titles/images ("image" key absent, not None) —
    # backfill once from the catalog stubs, no manifest calls needed.
    needs_backfill = bool(cached) and (
        "image" not in cached
        or any(not ep.get("title") or "image" not in ep
               for ep in cached.get("episodes", [])))
    fresh_cache = bool(cached and cached.get("episodes") and not needs_backfill
                       and time.time() - cached.get("fetched_at", 0)
                       < CATALOG_TTL_S)
    # STALE_OK first, fresh cache next, and only THEN probe connectivity —
    # so the 2s offline probe runs solely when we would otherwise hit psapi.
    if cached and cached.get("episodes") and (
            STALE_OK or fresh_cache or not _online()):
        age_h = (time.time() - cached.get("fetched_at", 0)) / 3600
        reason = ("accepted (playback)" if STALE_OK
                  else "fresh" if fresh_cache else "offline")
        _log(f"{slug}: catalog cache {reason} "
             f"({len(cached['episodes'])} episodes, {age_h:.1f}h old) — no API calls")
        return cached["episodes"]

    image = (cached or {}).get("image")
    try:
        if cached and cached.get("episodes"):
            known = {ep["id"] for ep in cached["episodes"]}
            fresh = _new_episodes(slug, known, kind)
            if fresh:
                _log(f"{slug}: {len(fresh)} new episode(s) since last check: "
                     + ", ".join(s.get("title") or s["id"] for s in fresh[:5]))
            else:
                _log(f"{slug}: catalog re-checked — nothing new")
            episodes = cached["episodes"] + _resolve_manifests(fresh, kind)
            episodes = episodes[-MAX_EPISODES:]
            if needs_backfill:
                stubs = {s["id"]: s for s in _psapi_episodes(slug, kind)}
                for ep in episodes:
                    stub = stubs.get(ep["id"]) or {}
                    if not ep.get("title"):
                        ep["title"] = stub.get("title")
                    if "image" not in ep:
                        ep["image"] = stub.get("image")
                _log(f"{slug}: backfilled titles/images into the catalog cache")
        else:
            _log(f"{slug}: first fetch — walking full psapi {kind} catalog...")
            episodes = _resolve_manifests(_psapi_episodes(slug, kind), kind)
            _log(f"{slug}: catalog has {len(episodes)} playable episodes")
        if "image" not in (cached or {}):
            image = _series_image(slug, kind)
        if episodes:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"fetched_at": time.time(), "image": image,
                           "episodes": episodes}, f)
            os.replace(tmp, path)
            return episodes
    except OSError as e:
        _log(f"{slug}: psapi catalog failed ({e!r})")
    if cached:
        _log(f"{slug}: using stale catalog cache ({len(cached['episodes'])} episodes) — offline mode")
        return cached["episodes"]
    return []


def _catalog_image(slug, kind="podcast"):
    """Show-level artwork: the locally cached cover file when downloaded
    (works offline), else the URL remembered in the catalog cache."""
    local = os.path.join(CACHE_DIR, slug, "cover.jpg")
    if os.path.exists(local):
        return local
    prefix = "catalog" if kind == "podcast" else f"catalog-{kind}"
    try:
        with open(os.path.join(CACHE_DIR, f"{prefix}-{slug}.json")) as f:
            return json.load(f).get("image")
    except (OSError, ValueError):
        return None


# Channel image of the last RSS feed expanded in this process, keyed by
# target URL — lets collection_image() answer for feeds without refetching.
_FEED_IMAGES = {}


def feed_key(target):
    """Stable cache-directory name for a generic RSS feed URL."""
    return "feed-" + hashlib.sha1(target.encode()).hexdigest()[:12]


def _nrk_slug(target):
    """The NRK podkast/serie slug for a target, or None."""
    for pat in (r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)",
                r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$"):
        m = re.match(pat, target, re.I)
        if m:
            return m.group(1)
    return None


def newest_episode_id(target):
    """The id of the newest episode in a podcast/series' CACHED listing,
    or None (not a feed, or nothing cached yet). Reads only local files —
    the sweeper calls it right after a sync to spot fresh content."""
    if target.startswith("storytel:"):
        from vibb import storytel
        return storytel.newest_book_id(target)
    slug = _nrk_slug(target)
    if slug:
        for prefix in ("catalog", "catalog-series"):
            try:
                with open(os.path.join(CACHE_DIR,
                                       f"{prefix}-{slug}.json")) as f:
                    eps = json.load(f).get("episodes") or []
                if eps:
                    return eps[-1].get("id")  # oldest-first -> last is newest
            except (OSError, ValueError):
                continue
        return None
    if target.startswith(("http://", "https://")):
        try:
            with open(os.path.join(CACHE_DIR, feed_key(target),
                                   "feed.json")) as f:
                items = json.load(f).get("items") or []
            if items:
                return _feed_episode_id(items[0][0])  # newest first
        except (OSError, ValueError):
            pass
    return None


def cache_key_for(target):
    """The CACHE_DIR subdirectory an entry's downloads live under, or None
    for targets we never cache (Spotify, local folders)."""
    slug = _nrk_slug(target)
    if slug:
        return slug
    if target.startswith("storytel:"):
        # CRITICAL: without this, cache_key_for returns None for a
        # storytel target, prune_cache finds the download dir ownerless,
        # and shutil.rmtree deletes gigabytes of audiobooks on the NEXT
        # PUT /library. Must match storytel.cache_dir's basename exactly.
        return "storytel-" + hashlib.sha1(target.encode()).hexdigest()[:12]
    if target.startswith(("http://", "https://")):
        return feed_key(target)
    return None


def prune_cache(keep_targets):
    """Delete cached episodes/catalogs that no entry wants offline anymore —
    entries removed from the library, or flipped to 'no offline'. Only ever
    removes orphans (dirs / catalog files under CACHE_DIR with no owner in
    keep_targets); the live library is the source of truth. Returns the list
    of removed names."""
    keep_dirs, keep_json = set(), set()
    for t in keep_targets:
        key = cache_key_for(t)
        if key:
            keep_dirs.add(key)
        slug = _nrk_slug(t)
        if slug:  # a slug's catalog cache is either podcast or series
            keep_json.add(f"catalog-{slug}.json")
            keep_json.add(f"catalog-series-{slug}.json")
    removed = []
    try:
        names = os.listdir(CACHE_DIR)
    except OSError:
        return removed
    for name in names:
        path = os.path.join(CACHE_DIR, name)
        if os.path.isdir(path):
            # spotify covers live under their own dir with their own
            # cleanup (ensure_spotify_art) — keyed per entry, not per
            # cache setting, so this sweep must leave them alone. Same
            # for the screen's album-art disk cache (ui-art): it has its
            # own size-capped pruning in ui.py.
            if name not in keep_dirs and name not in (SPOTIFY_ART_DIR,
                                                      UI_ART_DIR,
                                                      UI_EMOJI_DIR):
                shutil.rmtree(path, ignore_errors=True)
                removed.append(name)
        elif name.startswith("catalog-") and name.endswith(".json"):
            if name not in keep_json:
                try:
                    os.remove(path)
                    removed.append(name)
                except OSError:
                    pass
    return removed


def _feed_episode_id(enclosure_url):
    return hashlib.sha1(enclosure_url.encode()).hexdigest()[:12]


# --- spotify covers (oEmbed) ------------------------------------------------------
# go-librespot's API has no playlist metadata, but Spotify's public oEmbed
# endpoint returns the collection artwork for any share link — for
# playlists that's the same 4-cover mosaic the official apps show.
# Downloaded once by the cache sweeper; menus only ever read the file.

SPOTIFY_ART_DIR = "spotify-art"  # under CACHE_DIR; prune_cache skips it
UI_ART_DIR = "ui-art"  # the screen's album-art disk cache; also skipped
UI_EMOJI_DIR = "ui-emoji"  # the screen's emoji sprite cache (ui.py) —
#                            tiny PNGs built once per emoji; pruning
#                            them here would force a re-render after
#                            every library save


def spotify_art_path(target):
    key = hashlib.sha1(target.encode()).hexdigest()[:12]
    return os.path.join(CACHE_DIR, SPOTIFY_ART_DIR, f"{key}.jpg")


def _is_spotify(target):
    return ("open.spotify.com" in target or target.startswith("spotify:")
            or "spotify.link/" in target)


def fetch_spotify_art(target):
    """Download the cover for one Spotify link via oEmbed (network!).
    Returns the local path, or None. Already-downloaded art is kept."""
    dest = spotify_art_path(target)
    if os.path.exists(dest):
        return dest
    url = target
    if url.startswith("spotify:"):  # oEmbed wants the share-link form
        parts = url.split(":")
        if len(parts) != 3:
            return None
        url = f"https://open.spotify.com/{parts[1]}/{parts[2]}"
    try:
        d = json.loads(_get("https://open.spotify.com/oembed?url="
                            + urllib.parse.quote(url, safe="")))
        thumb = d.get("thumbnail_url")
        if not thumb:
            return None
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _download_image(thumb, dest)
        _log(f"spotify art cached for {url}")
        return dest
    except (OSError, ValueError):
        return None


def ensure_spotify_art(targets):
    """Fetch missing covers for the library's Spotify entries and drop art
    whose entry is gone. Called from the cache sweeper (network is fine
    there); everything else reads the files via collection_image."""
    spot = [t for t in targets if _is_spotify(t)]
    keep = set()
    for t in spot:
        keep.add(os.path.basename(spotify_art_path(t)))
        fetch_spotify_art(t)
    art_dir = os.path.join(CACHE_DIR, SPOTIFY_ART_DIR)
    try:
        for name in os.listdir(art_dir):
            if name not in keep:
                try:
                    os.remove(os.path.join(art_dir, name))
                except OSError:
                    pass
    except OSError:
        pass


def collection_image(target):
    """Artwork for the collection a link points at (menu icon for the
    screen UI / PWA). Call after expand_entries(target). None when unknown.
    Local files / in-process caches only — never the network."""
    if _is_spotify(target):
        p = spotify_art_path(target)
        return p if os.path.exists(p) else None
    if target.startswith("storytel:"):
        from vibb import storytel
        return storytel.local_cover(target)   # local file only, per contract
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        return _catalog_image(m.group(1), "podcast")
    m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
    if m:
        return _catalog_image(m.group(1), "series")
    if target.startswith(("http://", "https://")):
        local = os.path.join(CACHE_DIR, feed_key(target), "cover.jpg")
        if os.path.exists(local):
            return local
        return _FEED_IMAGES.get(target)
    if os.path.isdir(target):
        for name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg"):
            p = os.path.join(target, name)
            if os.path.exists(p):
                return p
    return None


# --- per-track embedded art (local collections) ---------------------------------
# iTunes buys and CD rips carry the cover INSIDE each file (ID3 APIC /
# MP4 covr). One cover.jpg per folder is right for an album but wrong
# for a folder of loose singles — so each file's art is extracted once
# to .art/<file>.jpg (300px, the podcast-art cap) and each track shows
# its own. The folder cover stays the collection's face outward.

AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav")
ART_DIR = ".art"


def track_art_path(folder, fname):
    return os.path.join(folder, ART_DIR, fname + ".jpg")


def _track_art_neg(folder, fname):
    return os.path.join(folder, ART_DIR, fname + ".none")


def drop_track_art(folder, fname):
    """Remove one file's extracted art + marker (file deleted)."""
    for p in (track_art_path(folder, fname), _track_art_neg(folder, fname)):
        try:
            os.remove(p)
        except OSError:
            pass


def extract_track_art(folder, fname):
    """Embedded art -> .art/<fname>.jpg, once ever per file.

    NEVER rewrites an existing jpg — mtime keys the UI's disk thumbs,
    and an identical rewrite would invalidate every cached size (QA
    2026-08-13). A file with no picture stream gets a .none marker so
    the nightly heal never re-runs ffprobe/ffmpeg on it. The probe uses
    -show_streams (attached pictures are invisible to -show_format);
    ffmpeg only ever runs when the probe saw a picture."""
    dest = track_art_path(folder, fname)
    neg = _track_art_neg(folder, fname)
    if os.path.exists(dest):
        return True
    if os.path.exists(neg):
        return False
    src = os.path.join(folder, fname)
    try:
        os.makedirs(os.path.join(folder, ART_DIR), exist_ok=True)
    except OSError:
        return False
    has_pic = False
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", src],
            capture_output=True, text=True, timeout=30)
        for st in json.loads(r.stdout or "{}").get("streams") or []:
            if st.get("codec_type") == "video":
                has_pic = True
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if has_pic:
        try:
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", src, "-an",
                 "-frames:v", "1", "-update", "1",
                 "-vf", "scale='min(300,iw)':-2", dest],
                capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.getsize(dest) > 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            os.remove(dest)
        except OSError:
            pass
    try:
        open(neg, "w").close()
    except OSError:
        pass
    return False


def folder_art_pending(folder):
    """Any audio file with neither art nor a no-art marker? Pure stats —
    the sweep uses this to skip the heal subprocess entirely."""
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(f.lower().endswith(AUDIO_EXTS)
               and not os.path.exists(track_art_path(folder, f))
               and not os.path.exists(_track_art_neg(folder, f))
               for f in names)


def heal_folder_art(folder):
    """One pass for a whole folder: extract what's missing (hand-copied
    collections never went through the uploader) and prune art whose
    audio file is gone (files deleted outside the PWA). Idempotent —
    the second run does only stats."""
    try:
        names = os.listdir(folder)
    except OSError:
        return 0
    audio = [f for f in sorted(names) if f.lower().endswith(AUDIO_EXTS)]
    got = sum(1 for f in audio if extract_track_art(folder, f))
    art_d = os.path.join(folder, ART_DIR)
    try:
        keep = {f + ".jpg" for f in audio} | {f + ".none" for f in audio}
        for a in os.listdir(art_d):
            if a not in keep:
                try:
                    os.remove(os.path.join(art_d, a))
                except OSError:
                    pass
    except OSError:
        pass
    _log(f"track art: {got}/{len(audio)} covers in {folder}")
    return got


# While a remote renderer (Sonos) is active the box is a controller, not
# a player: a cached LOCAL PATH is unplayable for the speaker, so the
# original stream url must win — otherwise exactly the shows synced for
# offline are the ones that refuse to play remotely. vibbd flips this
# on renderer switches (and at boot from renderer.json); player.py runs
# in its own process and never sees it, so box playback keeps preferring
# the cache as always.
PREFER_REMOTE = False


def _local_or_remote(slug, ep, kind="podcast"):
    if PREFER_REMOTE:
        return ep["url"]
    local = _episode_file(slug, ep["id"], kind)
    return local if os.path.exists(local) else ep["url"]


def _image_local_or_remote(dirname, eid, remote):
    """The cached episode artwork when synced, else the remote URL."""
    if eid:
        local = _episode_image_file(dirname, eid)
        if os.path.exists(local):
            return local
    return remote


def _catalog_entry(slug, episode_id, kind="podcast"):
    try:
        return next((ep for ep in _catalog(slug, kind)
                     if ep["id"] == episode_id), {})
    except Exception:
        return {}


def _podcast(slug, episode_id=None):
    if episode_id:
        cat = _catalog_entry(slug, episode_id)
        title = cat.get("title")
        image = _image_local_or_remote(slug, episode_id, cat.get("image"))
        local = _episode_file(slug, episode_id)
        if os.path.exists(local):
            return [{"url": local, "title": title, "id": episode_id,
                     "image": image}]
        url = _manifest_url(episode_id, "podcast")
        if url:
            return [{"url": url, "title": title, "id": episode_id,
                     "image": image}]
        # fallback: match the episode id in the official RSS
        root = ET.fromstring(_get(f"https://podkast.nrk.no/program/{slug}.rss"))
        for item in root.findall("./channel/item"):
            if episode_id in ET.tostring(item, encoding="unicode"):
                enc = item.find("enclosure")
                if enc is not None and "url" in enc.attrib:
                    return [{"url": enc.attrib["url"], "title": title,
                             "id": episode_id, "image": image}]
        return []

    # Whole podcast: the official RSS is often truncated, so use the full
    # psapi catalog (cached). Cached episodes play from disk.
    entries = _queue(slug, "podcast", newest_first=True)
    if entries:
        return entries
    _log(f"{slug}: psapi gave nothing — falling back to official RSS "
         "(often truncated to the last few episodes!)")
    chan_img, items = _parse_feed(_get(f"https://podkast.nrk.no/program/{slug}.rss"))
    return [{"url": u, "title": t, "id": None, "image": img or chan_img}
            for u, t, img in items]


def _queue(slug, kind, newest_first):
    """[(url, title)] queue for a whole podcast/series, with logging."""
    episodes = _catalog(slug, kind)
    if not episodes:
        return []
    if newest_first:
        episodes = list(reversed(episodes))
    urls = [_local_or_remote(slug, ep, kind) for ep in episodes]
    n_local = sum(1 for u in urls if u.startswith("/"))
    order = "newest first" if newest_first else "oldest first"
    _log(f"{slug}: queueing {len(urls)} episodes, {order} "
         f"({n_local} from local cache, {len(urls) - n_local} streamed)")
    if n_local < len(urls):
        # the per-episode listing earns its keep only when something will
        # stream — all-cached queues were 36 journal lines per expansion,
        # written several times per play on a Zero's SD card
        for i, (ep, u) in enumerate(zip(episodes, urls), 1):
            mark = "  [cached]" if u.startswith("/") else ""
            _log(f"  {i:3d}. {ep.get('title') or ep['id']}{mark}")
    return [{"url": u, "title": ep.get("title"), "id": ep["id"],
             "image": _image_local_or_remote(slug, ep["id"],
                                             ep.get("image"))}
            for ep, u in zip(episodes, urls)]


def podcast_slug(target):
    """The podcast slug from a radio.nrk.no/podkast link, else None."""
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    return m.group(1) if m else None


def _download(url, dest, timeout=120, *, headers=None, resume=False):
    """Stream url -> dest via a .part temp then atomic rename.

    `resume`: continue an existing .part with a Range request rather than
    starting over. A podcast episode is ~20MB and re-fetching is pennies;
    a 128k audiobook is 200-500MB and the sweep kills every download the
    moment playback starts, so without resume a big book on a Zero 2 W's
    shared radio might never finish. The signed url is single-use per
    call, so the CALLER re-mints a fresh url before resuming (the CDN
    path is stable; only the token changes). `headers` is keyword-only so
    the existing monkeypatched test stubs (lambda url, dest, **k) absorb
    it untouched."""
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if resume and os.path.exists(tmp) else 0
    hdrs = dict(headers or {})
    if have:
        hdrs["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # a server that ignored the Range answers 200 from the top — then
        # appending would corrupt the file, so restart in that case
        append = have and r.status == 206
        with open(tmp, "ab" if append else "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    os.replace(tmp, dest)


def _episode_image_file(dirname, eid):
    """Cached per-episode artwork, next to the episode's audio file."""
    return os.path.join(CACHE_DIR, dirname, f"{eid}.jpg")


def _download_image(url, dest, size=300):
    """Fetch artwork downscaled to <=size px — podcast art is often
    1500-3000px, ~50x what the 240px screen and PWA thumbnails need.
    Falls back to storing the original bytes when PIL is unavailable."""
    raw = _get(url, timeout=30)
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((size, size))
        img.save(dest + ".part", "JPEG", quality=85)
    except Exception:
        with open(dest + ".part", "wb") as f:
            f.write(raw)
    # fsync before the rename: os.replace alone is atomic against crashes of
    # THIS process, but a hard power cut can replay ext4 with the rename
    # durable and the data blocks empty — a corrupt jpg that then blocks
    # refetch forever because it exists (field 2026-07-23, RuntimeError on
    # every decode of an episode jpg).
    try:
        fd = os.open(dest + ".part", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    os.replace(dest + ".part", dest)


def shrink_covers(max_px=400, size=300):
    """Downscale covers that were stored full-size before _download_image
    handled them — decoding a 3000px JPEG per tile made the first pass
    through the carousel crawl. Idempotent and cheap when there is
    nothing to do (PIL reads just the header for the size check)."""
    try:
        from PIL import Image
    except ImportError:
        return 0
    paths = glob.glob(os.path.join(CACHE_DIR, "*", "cover.jpg"))
    paths += glob.glob(os.path.join(CACHE_DIR, SPOTIFY_ART_DIR, "*.jpg"))
    n = 0
    for p in paths:
        try:
            with Image.open(p) as img:
                if max(img.size) <= max_px:
                    continue
                img = img.convert("RGB")
                img.thumbnail((size, size))
                img.save(p + ".part", "JPEG", quality=85)
            os.replace(p + ".part", p)
            n += 1
        except (OSError, ValueError):
            continue
    if n:
        _log(f"shrunk {n} oversized cover(s) to <={size}px")
    return n


def _sync_episode_images(dirname, wanted):
    """Cache the per-episode artwork for the episodes kept offline —
    [(eid, image_url)] — so now-playing shows THE episode's picture
    even offline, not just the show cover."""
    n = 0
    for eid, src in wanted:
        if not src or not str(src).startswith("http"):
            continue
        dest = _episode_image_file(dirname, eid)
        if os.path.exists(dest):
            continue
        try:
            _download_image(src, dest)
            n += 1
        except OSError:
            pass
    if n:
        _log(f"{dirname}: cached {n} episode image(s)")


def sync_feed(target, count=SYNC_COUNT):
    """Download the first <count> episodes of a generic RSS feed (feeds list
    newest first by convention) to the cache, plus the channel cover."""
    chan_img, items = _parse_feed(_get(target))
    key = feed_key(target)
    os.makedirs(os.path.join(CACHE_DIR, key), exist_ok=True)
    # Persist the listing too, so offline browse/playback works even if this
    # feed was only ever synced, never opened online (expand also writes it).
    try:
        feed_cache = os.path.join(CACHE_DIR, key, "feed.json")
        with open(feed_cache + ".tmp", "w") as f:
            json.dump({"image": chan_img, "items": items}, f)
        os.replace(feed_cache + ".tmp", feed_cache)
    except OSError:
        pass
    cover = os.path.join(CACHE_DIR, key, "cover.jpg")
    if chan_img and not os.path.exists(cover):
        try:
            _download_image(chan_img, cover)
            _log(f"{key}: downloaded cover art")
        except OSError:
            pass
    wanted = items if count < 0 else items[:count]  # count<0 = keep all
    have = sum(1 for u, _t, _i in wanted if os.path.exists(
        os.path.join(CACHE_DIR, key, f"{_feed_episode_id(u)}.mp3")))
    _log(f"{key}: sync (feed) — {len(wanted)} newest wanted, {have} already "
         f"cached, {len(wanted) - have} to download")
    _sync_episode_images(key, [(_feed_episode_id(u), img)
                               for u, _t, img in wanted])
    for u, t, _img in wanted:
        dest = os.path.join(CACHE_DIR, key, f"{_feed_episode_id(u)}.mp3")
        if os.path.exists(dest):
            continue
        try:
            _download(u, dest)
            print(f"cached {key}/{t or u}", flush=True)
        except OSError as e:
            print(f"failed {key}/{u}: {e}", flush=True)
            try:
                os.remove(dest + ".part")
            except OSError:
                pass


def sync(slug, count=SYNC_COUNT, kind="podcast"):
    """Download the newest <count> episodes to the cache, newest first.
    Podcasts are straight mp3 downloads; series episodes (HLS) are captured
    to m4a with ffmpeg. Already-cached episodes are skipped."""
    episodes = _catalog(slug, kind)  # oldest first
    wanted = episodes if count < 0 else episodes[-count:]  # count<0 = keep all
    have = sum(1 for ep in wanted
               if os.path.exists(_episode_file(slug, ep["id"], kind)))
    _log(f"{slug}: sync ({kind}) — {len(wanted)} newest wanted, {have} already "
         f"cached, {len(wanted) - have} to download")
    os.makedirs(os.path.join(CACHE_DIR, slug), exist_ok=True)
    # Show artwork for offline menus: one small jpg next to the episodes
    cover = os.path.join(CACHE_DIR, slug, "cover.jpg")
    img_url = _catalog_image(slug, kind)
    if not os.path.exists(cover) and img_url and img_url.startswith("http"):
        try:
            _download_image(img_url, cover)
            _log(f"{slug}: downloaded cover art")
        except OSError:
            pass
    _sync_episode_images(slug, [(ep["id"], ep.get("image"))
                                for ep in wanted])
    for ep in reversed(wanted):  # newest first
        dest = _episode_file(slug, ep["id"], kind)
        if os.path.exists(dest):
            continue
        tmp = dest + ".part"
        try:
            if kind == "podcast":
                _download(ep["url"], dest)
                print(f"cached {slug}/{ep['id']}", flush=True)
                continue
            else:  # HLS -> single m4a file, no re-encode
                result = subprocess.run(
                    ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                     "-i", ep["url"], "-c", "copy", "-f", "mp4", tmp],
                    timeout=900)
                if result.returncode != 0:
                    raise OSError(f"ffmpeg exited {result.returncode}")
            os.replace(tmp, dest)
            print(f"cached {slug}/{ep['id']}", flush=True)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"failed {slug}/{ep['id']}: {e}", flush=True)
            try:
                os.remove(tmp)
            except OSError:
                pass


def _series(first_program_id):
    entries, pid = [], first_program_id
    for _ in range(MAX_EPISODES):
        try:
            manifest = _get_json(f"{PSAPI}/playback/manifest/program/{pid}")
            assets = (manifest.get("playable") or {}).get("assets") or []
            if assets and assets[0].get("url"):
                entries.append({"url": assets[0]["url"], "title": None,
                                "id": pid, "image": None})
        except OSError:
            pass  # episode not playable (geo block, expired) — skip it
        try:
            meta = _get_json(f"{PSAPI}/playback/metadata/program/{pid}")
        except OSError:
            break
        href = ((meta.get("_links") or {}).get("next") or {}).get("href")
        if not href:
            break
        pid = href.rstrip("/").split("/")[-1]
    return entries


def expand(target):
    """Stream URLs for a link — [target] when it's not NRK/RSS."""
    return [e["url"] for e in expand_entries(target)]


def expand_titled(target):
    """[(stream_url, title_or_None)] — see expand_entries."""
    return [(e["url"], e.get("title")) for e in expand_entries(target)]


def expand_entries(target):
    """Turn an NRK/feed link into [{'url', 'title', 'id', 'image'}].

    'id' is the stable episode id when known (None otherwise) — playback
    state should key on it, since the same episode can be a stream URL one
    day and a cached local file the next. Returns a single passthrough
    entry when the link is not NRK/RSS or lookup fails, so the caller can
    always just play whatever comes back.
    """
    passthrough = [{"url": target, "title": None, "id": None, "image": None}]
    if target.startswith("storytel:"):
        # storytel owns its own on-disk format (shelf.json + local mp3s).
        # A LOCAL read only: download-only content, so a book not on disk
        # is omitted rather than streamed from an expiring signed url —
        # which keeps this branch network-free on the playback path.
        from vibb import storytel
        return storytel.entries_for(target)
    if os.path.isdir(target):
        # Local folder (e.g. a DRM-free audiobook): sorted playlist of audio
        # files, each keyed on its filename so resume survives moves/renames
        # of the parent folder. A cover.jpg in the folder becomes the art.
        files = sorted(f for f in os.listdir(target)
                       if f.lower().endswith(AUDIO_EXTS))
        if files:
            _log(f"folder with {len(files)} audio files: {target}")
            cover = collection_image(target)
            # Embedded tags, recorded by the uploader (daemon writes this
            # sidecar with ffprobe output). Titles beat filenames on a
            # 240px screen — "Kapittel 3" instead of "01-track_03_final".
            # Absent sidecar = the old filename behaviour, so folders
            # copied on by hand keep working exactly as before.
            meta = {}
            try:
                with open(os.path.join(target, ".vibb-meta.json")) as mf:
                    meta = json.load(mf)
            except (OSError, ValueError):
                pass
            if meta:
                # Order by embedded track number when EVERY file has one;
                # a partial set would interleave worse than the filenames.
                nums = [meta.get(f, {}).get("track") for f in files]
                if all(isinstance(n, int) for n in nums):
                    files = [f for _, f in sorted(zip(nums, files))]
            # Per-track art beats the folder cover: a folder of loose
            # singles (each mp3 with its own embedded art) shows each
            # song's OWN cover in the list and the now view. An album
            # whose files all carry the same art looks unchanged. The
            # exists-probe (not the meta sidecar) is deliberate: it
            # self-heals on delete/rename and works for hand-copied
            # folders (QA 2026-08-13).
            def _img(f):
                art = track_art_path(target, f)
                return art if os.path.exists(art) else cover
            return [{"url": os.path.join(target, f),
                     "title": (meta.get(f, {}).get("title")
                               or os.path.splitext(f)[0]),
                     "id": f, "image": _img(f)}
                    for f in files]
        return passthrough
    try:
        m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)/([A-Za-z0-9_-]+)/?$",
                     target, re.I)
        if m:
            return _podcast(m.group(1), m.group(2)) or passthrough
        m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            return _podcast(m.group(1)) or passthrough
        m = re.match(r"https?://radio\.nrk\.no/direkte/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            # Live radio channel: resolve the HLS stream from the psapi
            # channel manifest. Continuous — no resume, no cache.
            chan = m.group(1)
            try:
                d = _get_json(f"{PSAPI}/playback/manifest/channel/{chan}")
                assets = (d.get("playable") or {}).get("assets") or []
                if assets and assets[0].get("url"):
                    _log(f"live radio: {chan}")
                    return [{"url": assets[0]["url"], "title": f"NRK {chan}",
                             "id": None, "image": None}]
            except (OSError, ValueError):
                pass
            return passthrough
        m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            # Bare series link: whole series via the psapi catalog, episode 1
            # first (radio series are serial stories — chronological order)
            return _queue(m.group(1), "series", newest_first=False) or passthrough
        m = re.match(r"https?://radio\.nrk\.no/serie/[^/]+/([A-Za-z0-9_-]+)/?$",
                     target, re.I)
        if m:
            return _series(m.group(1)) or passthrough
    except (OSError, ET.ParseError):
        return passthrough  # lookup failed — let mpv+yt-dlp try the raw link
    if target.startswith(("http://", "https://")):
        key = feed_key(target)
        feed_cache = os.path.join(CACHE_DIR, key, "feed.json")
        have_cache = os.path.exists(feed_cache)
        # Playback (STALE_OK) always trusts the cache and NEVER probes; the
        # menu probes once, only when it might reach for the network, and
        # trusts the cache instead when offline. Offline-first either way:
        # a doomed fetch just times out (8s+) and falls back here anyway.
        cache_first = STALE_OK or (have_cache and not _online())
        looks_like_feed = (
            target.lower().split("?")[0].endswith((".rss", ".xml"))
            or have_cache  # known feed — works offline
            # sniff an unknown URL only when browsing online: playback never
            # sniffs (unknown -> passthrough), and offline it would just hang
            or (not STALE_OK and _online() and _sniffs_like_feed(target)))
        if looks_like_feed:
            chan_img, items = None, []
            if cache_first and have_cache:
                try:
                    with open(feed_cache) as f:
                        d = json.load(f)
                    chan_img = d.get("image")
                    items = [tuple(i) for i in d["items"]]
                except (OSError, ValueError, KeyError):
                    items = []
            try:
                if not items and not cache_first:
                    chan_img, items = _parse_feed(_get(target))
                if items and not cache_first:  # remember for offline replays
                    os.makedirs(os.path.dirname(feed_cache), exist_ok=True)
                    with open(feed_cache + ".tmp", "w") as f:
                        json.dump({"image": chan_img, "items": items}, f)
                    os.replace(feed_cache + ".tmp", feed_cache)
            except (OSError, ET.ParseError) as e:
                try:
                    with open(feed_cache) as f:
                        d = json.load(f)
                    chan_img, items = d.get("image"), [tuple(i) for i in d["items"]]
                    _log(f"feed fetch failed ({e!r}) — using cached listing "
                         f"({len(items)} episodes), offline mode")
                except (OSError, ValueError, KeyError):
                    _log(f"feed parse failed ({e!r}) — passing link to mpv: {target}")
            if items:
                _log(f"RSS feed with {len(items)} episodes: {target}")
                if chan_img:
                    _FEED_IMAGES[target] = chan_img
                out = []
                for u, t, img in items:
                    eid = _feed_episode_id(u)
                    local = os.path.join(CACHE_DIR, key, f"{eid}.mp3")
                    use = (u if PREFER_REMOTE
                           else (local if os.path.exists(local) else u))
                    out.append({"url": use,
                                "title": t, "id": eid,
                                "image": _image_local_or_remote(
                                    key, eid, img or chan_img)})
                n_local = sum(1 for e in out if not e["url"].startswith("http"))
                if n_local:
                    _log(f"  {n_local} episode(s) from local cache")
                return out
    return passthrough


def _sniffs_like_feed(url):
    """Peek at the first bytes: does this URL serve RSS/XML? Works for feed
    URLs without .rss/.xml extensions (acast, anchor, WordPress /feed/...)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vibb/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            head = r.read(2048)
        s = head.lstrip()[:300].lower()
        return s.startswith(b"<?xml") or b"<rss" in s
    except OSError:
        return False


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "sync":
        sync(sys.argv[2],
             int(sys.argv[3]) if len(sys.argv) > 3 else SYNC_COUNT,
             sys.argv[4] if len(sys.argv) > 4 else "podcast")
    elif len(sys.argv) >= 3 and sys.argv[1] == "sync-feed":
        sync_feed(sys.argv[2],
                  int(sys.argv[3]) if len(sys.argv) > 3 else SYNC_COUNT)
    elif len(sys.argv) == 3 and sys.argv[1] == "art-heal":
        heal_folder_art(sys.argv[2])
    elif len(sys.argv) == 2:
        print("\n".join(expand(sys.argv[1])))
    else:
        print("usage: nrk.py <link>  |  nrk.py sync <slug> [count] [kind]"
              "  |  nrk.py sync-feed <url> [count]", file=sys.stderr)
        sys.exit(1)
