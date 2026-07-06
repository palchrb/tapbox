"""go-librespot client — the ONE place that talks to the Spotify daemon."""

import json
import os
import re
import time
import urllib.request

API = os.environ.get("TAPBOX_GO_API", "http://127.0.0.1:3678")

URI_RE = re.compile(
    r"^spotify:(track|album|playlist|artist|episode|show):[A-Za-z0-9]+$")
LINK_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z-]+/)?"
    r"(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)")


def is_spotify(target):
    return (target.startswith("spotify:") or "open.spotify.com" in target
            or "spotify.link/" in target)


def to_uri(target):
    """A share link/URI -> spotify:<type>:<id>, or None."""
    if URI_RE.match(target):
        return target
    if "spotify.link/" in target:  # short links redirect to open.spotify.com
        with urllib.request.urlopen(target, timeout=10) as r:
            target = r.url
    m = LINK_RE.search(target)
    return f"spotify:{m.group(1)}:{m.group(2)}" if m else None


def go(path, timeout=5, body=None):
    """POST to the go-librespot API. Raises OSError when unreachable."""
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def status(timeout=5):
    """The /status dict, {} when unreachable or not logged in."""
    try:
        with urllib.request.urlopen(API + "/status", timeout=timeout) as r:
            return json.loads(r.read()) or {}
    except (OSError, ValueError):
        return {}


def playing(st=None):
    st = status() if st is None else st
    return bool(st.get("track")) and not st.get("paused") and not st.get("stopped")


def command(action):
    """playpause/next/prev. Spotify's prev only rewinds the current track
    first; since a button is one gesture, send the second prev ourselves
    when the first one only rewound."""
    if action != "prev":
        go({"playpause": "/player/playpause", "next": "/player/next"}[action])
        return
    before = (status().get("track") or {}).get("uri")
    go("/player/prev")
    time.sleep(0.4)
    after = status().get("track") or {}
    # position is on the track object (ms) — same uri near 0 = only rewound
    if after.get("uri") == before and (after.get("position") or 0) < 2000:
        go("/player/prev")
