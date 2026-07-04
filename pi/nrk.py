"""NRK link expansion for the TapBox rig.

Ported from palchrb's rfid_sonos_backend app.py, minus the Sonos-specific
parts (x-sonos-http URIs, DIDL metadata). The strategy is the same:

- radio.nrk.no/serie/<serie>/<programId>: walk psapi metadata _links.next
  to queue the whole series from that episode, resolving each episode's
  stream URL via the psapi playback manifest.
- radio.nrk.no/podkast/<slug>/<episodeId>: resolve the episode's mp3 via
  the psapi playback manifest (RSS enclosure match as fallback).
- radio.nrk.no/podkast/<slug>: play the whole catalog, oldest episode
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
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

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
    root = ET.fromstring(feed_bytes)
    return [item.find("enclosure").attrib["url"]
            for item in root.findall("./channel/item")
            if item.find("enclosure") is not None
            and "url" in item.find("enclosure").attrib]


def _podcast_manifest_url(episode_id):
    try:
        m = _get_json(f"{PSAPI}/playback/manifest/podcast/{episode_id}")
        assets = (m.get("playable") or {}).get("assets") or []
        if assets and assets[0].get("url"):
            return assets[0]["url"]
    except OSError:
        pass
    return None


def _psapi_podcast_episode_ids(slug):
    """Full episode id list from the psapi catalog, oldest first."""
    ids = []
    href = f"/radio/catalog/podcast/{slug}/episodes?page=1&pageSize=50&sort=asc"
    while href and len(ids) < MAX_EPISODES:
        data = _get_json(PSAPI + href)
        for ep in (data.get("_embedded") or {}).get("episodes") or []:
            self_href = ((ep.get("_links") or {}).get("self") or {}).get("href", "")
            eid = self_href.rstrip("/").split("/")[-1]
            if eid:
                ids.append(eid)
        href = ((data.get("_links") or {}).get("next") or {}).get("href")
    return ids[:MAX_EPISODES]


def _episode_file(slug, episode_id):
    return os.path.join(CACHE_DIR, slug, f"{episode_id}.mp3")


def _new_episode_ids(slug, known_ids):
    """IDs newer than anything we know, oldest first. Walks pages newest-first
    and stops at the first known id, so the steady-state cost is ONE call."""
    new = []
    href = f"/radio/catalog/podcast/{slug}/episodes?page=1&pageSize=50&sort=desc"
    while href and len(new) < MAX_EPISODES:
        data = _get_json(PSAPI + href)
        for ep in (data.get("_embedded") or {}).get("episodes") or []:
            self_href = ((ep.get("_links") or {}).get("self") or {}).get("href", "")
            eid = self_href.rstrip("/").split("/")[-1]
            if eid in known_ids:
                return list(reversed(new))
            if eid:
                new.append(eid)
        href = ((data.get("_links") or {}).get("next") or {}).get("href")
    return list(reversed(new))


def _resolve_manifests(ids):
    with ThreadPoolExecutor(max_workers=8) as ex:
        urls = list(ex.map(_podcast_manifest_url, ids))
    return [{"id": i, "url": u} for i, u in zip(ids, urls) if u]


def _podcast_catalog(slug):
    """[{id, url}] oldest first. Cached with a TTL. Refreshes are incremental:
    already-resolved episodes are reused (NRK's CDN URLs are stable), so one
    new episode costs 1 catalog call + 1 manifest call — not a full re-walk.
    A stale catalog beats nothing when the network is down (offline mode)."""
    path = os.path.join(CACHE_DIR, f"catalog-{slug}.json")
    cached = None
    try:
        with open(path) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        pass
    if cached and time.time() - cached.get("fetched_at", 0) < CATALOG_TTL_S:
        return cached["episodes"]

    try:
        if cached and cached.get("episodes"):
            known = {ep["id"] for ep in cached["episodes"]}
            episodes = cached["episodes"] + _resolve_manifests(_new_episode_ids(slug, known))
            episodes = episodes[-MAX_EPISODES:]
        else:
            episodes = _resolve_manifests(_psapi_podcast_episode_ids(slug))
        if episodes:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"fetched_at": time.time(), "episodes": episodes}, f)
            os.replace(tmp, path)
            return episodes
    except OSError:
        pass
    if cached:
        return cached["episodes"]
    return []


def _local_or_remote(slug, ep):
    local = _episode_file(slug, ep["id"])
    return local if os.path.exists(local) else ep["url"]


def _podcast(slug, episode_id=None):
    if episode_id:
        local = _episode_file(slug, episode_id)
        if os.path.exists(local):
            return [local]
        url = _podcast_manifest_url(episode_id)
        if url:
            return [url]
        # fallback: match the episode id in the official RSS
        root = ET.fromstring(_get(f"https://podkast.nrk.no/program/{slug}.rss"))
        for item in root.findall("./channel/item"):
            if episode_id in ET.tostring(item, encoding="unicode"):
                enc = item.find("enclosure")
                if enc is not None and "url" in enc.attrib:
                    return [enc.attrib["url"]]
        return []

    # Whole podcast: the official RSS is often truncated, so use the full
    # psapi catalog (cached). Cached episodes play from disk.
    episodes = _podcast_catalog(slug)
    if episodes:
        return [_local_or_remote(slug, ep) for ep in episodes]
    return _feed_enclosures(_get(f"https://podkast.nrk.no/program/{slug}.rss"))


def podcast_slug(target):
    """The podcast slug from a radio.nrk.no/podkast link, else None."""
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    return m.group(1) if m else None


def sync(slug, count=SYNC_COUNT):
    """Download the newest <count> episodes to the cache, newest first.
    Already-cached episodes are skipped, so this is cheap to re-run."""
    episodes = _podcast_catalog(slug)  # oldest first
    os.makedirs(os.path.join(CACHE_DIR, slug), exist_ok=True)
    for ep in reversed(episodes[-count:]):  # newest first
        dest = _episode_file(slug, ep["id"])
        if os.path.exists(dest):
            continue
        tmp = dest + ".part"
        try:
            with urllib.request.urlopen(ep["url"], timeout=120) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dest)
            print(f"cached {slug}/{ep['id']}", flush=True)
        except OSError as e:
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
    """Turn an NRK/feed link into a list of stream URLs for mpv.

    Returns [target] unchanged when the link is not NRK/RSS or lookup fails,
    so the caller can always just play whatever comes back.
    """
    try:
        m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)/([A-Za-z0-9_-]+)/?$",
                     target, re.I)
        if m:
            return _podcast(m.group(1), m.group(2)) or [target]
        m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            return _podcast(m.group(1)) or [target]
        m = re.match(r"https?://radio\.nrk\.no/serie/[^/]+/([A-Za-z0-9_-]+)/?$",
                     target, re.I)
        if m:
            return _series(m.group(1)) or [target]
    except (OSError, ET.ParseError):
        return [target]  # lookup failed — let mpv+yt-dlp have a go at the raw link
    if target.lower().split("?")[0].endswith((".rss", ".xml")):
        try:
            return _feed_enclosures(_get(target)) or [target]
        except (OSError, ET.ParseError):
            return [target]
    return [target]


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "sync":
        sync(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else SYNC_COUNT)
    elif len(sys.argv) == 2:
        print("\n".join(expand(sys.argv[1])))
    else:
        print("usage: nrk.py <link>  |  nrk.py sync <slug> [count]", file=sys.stderr)
        sys.exit(1)
