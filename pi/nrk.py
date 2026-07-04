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


def _feed_enclosures(feed_bytes):
    """[(url, title)] for every enclosure in an RSS feed."""
    pairs = []
    root = ET.fromstring(feed_bytes)
    for item in root.findall("./channel/item"):
        enc = item.find("enclosure")
        if enc is None or "url" not in enc.attrib:
            continue
        t = item.find("title")
        title = t.text.strip() if t is not None and t.text else None
        pairs.append((enc.attrib["url"], title))
    return pairs


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
    return {"id": eid, "title": title} if eid else None


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
    if cached and time.time() - cached.get("fetched_at", 0) < CATALOG_TTL_S:
        age_h = (time.time() - cached["fetched_at"]) / 3600
        _log(f"{slug}: catalog cache fresh ({len(cached['episodes'])} episodes, {age_h:.1f}h old) — no API calls")
        return cached["episodes"]

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
        else:
            _log(f"{slug}: first fetch — walking full psapi {kind} catalog...")
            episodes = _resolve_manifests(_psapi_episodes(slug, kind), kind)
            _log(f"{slug}: catalog has {len(episodes)} playable episodes")
        if episodes:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"fetched_at": time.time(), "episodes": episodes}, f)
            os.replace(tmp, path)
            return episodes
    except OSError as e:
        _log(f"{slug}: psapi catalog failed ({e!r})")
    if cached:
        _log(f"{slug}: using stale catalog cache ({len(cached['episodes'])} episodes) — offline mode")
        return cached["episodes"]
    return []


def _local_or_remote(slug, ep, kind="podcast"):
    local = _episode_file(slug, ep["id"], kind)
    return local if os.path.exists(local) else ep["url"]


def _catalog_title(slug, episode_id, kind="podcast"):
    try:
        return next((ep.get("title") for ep in _catalog(slug, kind)
                     if ep["id"] == episode_id), None)
    except Exception:
        return None


def _podcast(slug, episode_id=None):
    if episode_id:
        title = _catalog_title(slug, episode_id)
        local = _episode_file(slug, episode_id)
        if os.path.exists(local):
            return [(local, title)]
        url = _manifest_url(episode_id, "podcast")
        if url:
            return [(url, title)]
        # fallback: match the episode id in the official RSS
        root = ET.fromstring(_get(f"https://podkast.nrk.no/program/{slug}.rss"))
        for item in root.findall("./channel/item"):
            if episode_id in ET.tostring(item, encoding="unicode"):
                enc = item.find("enclosure")
                if enc is not None and "url" in enc.attrib:
                    return [(enc.attrib["url"], title)]
        return []

    # Whole podcast: the official RSS is often truncated, so use the full
    # psapi catalog (cached). Cached episodes play from disk.
    pairs = _queue(slug, "podcast", newest_first=True)
    if pairs:
        return pairs
    _log(f"{slug}: psapi gave nothing — falling back to official RSS "
         "(often truncated to the last few episodes!)")
    return _feed_enclosures(_get(f"https://podkast.nrk.no/program/{slug}.rss"))


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
    return [(u, ep.get("title")) for ep, u in zip(episodes, urls)]


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
    urls, pid = [], first_program_id
    for _ in range(MAX_EPISODES):
        try:
            manifest = _get_json(f"{PSAPI}/playback/manifest/program/{pid}")
            assets = (manifest.get("playable") or {}).get("assets") or []
            if assets and assets[0].get("url"):
                urls.append(assets[0]["url"])
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
    return urls


def expand(target):
    """Stream URLs for a link — [target] when it's not NRK/RSS."""
    return [u for u, _ in expand_titled(target)]


def expand_titled(target):
    """Turn an NRK/feed link into [(stream_url, title_or_None)] for mpv.

    Returns [(target, None)] unchanged when the link is not NRK/RSS or
    lookup fails, so the caller can always just play whatever comes back.
    """
    try:
        m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)/([A-Za-z0-9_-]+)/?$",
                     target, re.I)
        if m:
            return _podcast(m.group(1), m.group(2)) or [(target, None)]
        m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            return _podcast(m.group(1)) or [(target, None)]
        m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            # Bare series link: whole series via the psapi catalog, episode 1
            # first (radio series are serial stories — chronological order)
            return _queue(m.group(1), "series", newest_first=False) or [(target, None)]
        m = re.match(r"https?://radio\.nrk\.no/serie/[^/]+/([A-Za-z0-9_-]+)/?$",
                     target, re.I)
        if m:
            return [(u, None) for u in _series(m.group(1))] or [(target, None)]
    except (OSError, ET.ParseError):
        return [(target, None)]  # lookup failed — let mpv+yt-dlp try the raw link
    if target.startswith(("http://", "https://")):
        try:
            if target.lower().split("?")[0].endswith((".rss", ".xml")) or _sniffs_like_feed(target):
                pairs = _feed_enclosures(_get(target))
                if pairs:
                    _log(f"RSS feed with {len(pairs)} episodes: {target}")
                    return pairs
        except (OSError, ET.ParseError) as e:
            _log(f"feed parse failed ({e!r}) — passing link to mpv: {target}")
    return [(target, None)]


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
