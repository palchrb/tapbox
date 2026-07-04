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

Everything is fetched on demand — no local feed files to maintain on the
device. Unknown URLs pass through untouched (mpv + yt-dlp handles them).
"""

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

PSAPI = "https://psapi.nrk.no"
MAX_EPISODES = 100


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


def _podcast(slug, episode_id=None):
    if episode_id:
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

    # Whole podcast: the official RSS is often truncated, so prefer the full
    # psapi catalog and resolve stream URLs in parallel (order preserved).
    try:
        ids = _psapi_podcast_episode_ids(slug)
        if ids:
            with ThreadPoolExecutor(max_workers=8) as ex:
                urls = [u for u in ex.map(_podcast_manifest_url, ids) if u]
            if urls:
                return urls
    except OSError:
        pass
    return _feed_enclosures(_get(f"https://podkast.nrk.no/program/{slug}.rss"))


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
