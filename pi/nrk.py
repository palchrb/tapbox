"""NRK link expansion for the TapBox rig.

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

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor


def _log(msg):
    print(f"nrk: {msg}", file=sys.stderr, flush=True)

PSAPI = "https://psapi.nrk.no"
MAX_EPISODES = 100
CACHE_DIR = os.environ.get("TAPBOX_CACHE", "/var/lib/tapbox/cache")
CATALOG_TTL_S = 12 * 3600
SYNC_COUNT = 50


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "tapbox/0.1"})
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
    if (cached and not needs_backfill
            and time.time() - cached.get("fetched_at", 0) < CATALOG_TTL_S):
        age_h = (time.time() - cached["fetched_at"]) / 3600
        _log(f"{slug}: catalog cache fresh ({len(cached['episodes'])} episodes, {age_h:.1f}h old) — no API calls")
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


def collection_image(target):
    """Artwork for the collection a link points at (menu icon for the
    screen UI / PWA). Call after expand_entries(target). None when unknown."""
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        return _catalog_image(m.group(1), "podcast")
    m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
    if m:
        return _catalog_image(m.group(1), "series")
    if os.path.isdir(target):
        for name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg"):
            p = os.path.join(target, name)
            if os.path.exists(p):
                return p
        return None
    return _FEED_IMAGES.get(target)


def _local_or_remote(slug, ep, kind="podcast"):
    local = _episode_file(slug, ep["id"], kind)
    return local if os.path.exists(local) else ep["url"]


def _catalog_entry(slug, episode_id, kind="podcast"):
    try:
        return next((ep for ep in _catalog(slug, kind)
                     if ep["id"] == episode_id), {})
    except Exception:
        return {}


def _podcast(slug, episode_id=None):
    if episode_id:
        cat = _catalog_entry(slug, episode_id)
        title, image = cat.get("title"), cat.get("image")
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
         f"({n_local} from local cache, {len(urls) - n_local} streamed):")
    for i, (ep, u) in enumerate(zip(episodes, urls), 1):
        mark = "  [cached]" if u.startswith("/") else ""
        _log(f"  {i:3d}. {ep.get('title') or ep['id']}{mark}")
    return [{"url": u, "title": ep.get("title"), "id": ep["id"],
             "image": ep.get("image")}
            for ep, u in zip(episodes, urls)]


def podcast_slug(target):
    """The podcast slug from a radio.nrk.no/podkast link, else None."""
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    return m.group(1) if m else None


def sync(slug, count=SYNC_COUNT, kind="podcast"):
    """Download the newest <count> episodes to the cache, newest first.
    Podcasts are straight mp3 downloads; series episodes (HLS) are captured
    to m4a with ffmpeg. Already-cached episodes are skipped."""
    episodes = _catalog(slug, kind)  # oldest first
    wanted = episodes[-count:]
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
            with open(cover + ".part", "wb") as f:
                f.write(_get(img_url, timeout=30))
            os.replace(cover + ".part", cover)
            _log(f"{slug}: downloaded cover art")
        except OSError:
            pass
    for ep in reversed(wanted):  # newest first
        dest = _episode_file(slug, ep["id"], kind)
        if os.path.exists(dest):
            continue
        tmp = dest + ".part"
        try:
            if kind == "podcast":
                with urllib.request.urlopen(ep["url"], timeout=120) as r, \
                        open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
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
    if os.path.isdir(target):
        # Local folder (e.g. a DRM-free audiobook): sorted playlist of audio
        # files, each keyed on its filename so resume survives moves/renames
        # of the parent folder. A cover.jpg in the folder becomes the art.
        exts = (".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav")
        files = sorted(f for f in os.listdir(target)
                       if f.lower().endswith(exts))
        if files:
            _log(f"folder with {len(files)} audio files: {target}")
            cover = collection_image(target)
            return [{"url": os.path.join(target, f),
                     "title": os.path.splitext(f)[0], "id": f, "image": cover}
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
        try:
            if target.lower().split("?")[0].endswith((".rss", ".xml")) or _sniffs_like_feed(target):
                chan_img, items = _parse_feed(_get(target))
                if items:
                    _log(f"RSS feed with {len(items)} episodes: {target}")
                    if chan_img:
                        _FEED_IMAGES[target] = chan_img
                    return [{"url": u, "title": t, "id": None,
                             "image": img or chan_img} for u, t, img in items]
        except (OSError, ET.ParseError) as e:
            _log(f"feed parse failed ({e!r}) — passing link to mpv: {target}")
    return passthrough


def _sniffs_like_feed(url):
    """Peek at the first bytes: does this URL serve RSS/XML? Works for feed
    URLs without .rss/.xml extensions (acast, anchor, WordPress /feed/...)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tapbox/0.1"})
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
    elif len(sys.argv) == 2:
        print("\n".join(expand(sys.argv[1])))
    else:
        print("usage: nrk.py <link>  |  nrk.py sync <slug> [count]", file=sys.stderr)
        sys.exit(1)
