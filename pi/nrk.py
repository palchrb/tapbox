"""NRK link expansion for the TapBox rig.

Ported from palchrb's rfid_sonos_backend app.py, minus the Sonos-specific
parts (x-sonos-http URIs, DIDL metadata). The strategy is the same:

- radio.nrk.no/serie/<serie>/<programId>: walk psapi metadata _links.next
  to queue the whole series from that episode, resolving each episode's
  stream URL via the psapi playback manifest.
- radio.nrk.no/podkast/<slug>/<episodeId>: look up the episode's mp3
  enclosure in the official RSS feed (podkast.nrk.no) by matching the
  episode id, instead of scraping titles from the episode page.
- radio.nrk.no/podkast/<slug>: play the whole feed.
- Any URL ending in .rss/.xml (e.g. an nrk-pod-feeds mirror): play all
  enclosures.

Everything is fetched on demand — no local feed files to maintain on the
device. Unknown URLs pass through untouched (mpv + yt-dlp handles them).
"""

import json
import re
import urllib.request
import xml.etree.ElementTree as ET

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


def _podcast(slug, episode_id=None):
    feed = _get(f"https://podkast.nrk.no/program/{slug}.rss")
    if not episode_id:
        return _feed_enclosures(feed)
    root = ET.fromstring(feed)
    for item in root.findall("./channel/item"):
        if episode_id in ET.tostring(item, encoding="unicode"):
            enc = item.find("enclosure")
            if enc is not None and "url" in enc.attrib:
                return [enc.attrib["url"]]
    return []


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
    if target.lower().split("?")[0].endswith((".rss", ".xml")):
        try:
            return _feed_enclosures(_get(target)) or [target]
        except (OSError, ET.ParseError):
            return [target]
    return [target]
