"""Spotify Web API client (client-credentials) — public data only.

Powers "follow a profile": a library section subscribed to a Spotify
username is kept in sync with that profile's PUBLIC playlists. Client
credentials never involve a user login, so one free app registration at
developer.spotify.com serves every box in the household; the secret can
only read what anyone can already see on open.spotify.com.

Credentials live in a root-owned JSON file ({"client_id", "client_secret"}),
written once by install.sh. Without the file everything here degrades to
"not configured" — the rest of the box is unaffected."""

import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request

CREDS_FILE = os.environ.get("VIBB_SPOTIFY_API",
                            "/etc/vibb/spotify-api.json")
ACCOUNTS = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

_USER_RE = re.compile(r"open\.spotify\.com/user/([^/?#]+)")


def parse_user(s):
    """A profile share link, spotify:user: URI or plain username -> the
    username, or None. Usernames are treated as opaque (old accounts can
    contain almost anything); we just strip the URL forms around them."""
    s = str(s or "").strip()
    m = _USER_RE.search(s)
    if m:
        s = m.group(1)
    elif s.startswith("spotify:user:"):
        s = s[len("spotify:user:"):]
    s = urllib.parse.unquote(s).strip()
    return s or None


def credentials():
    try:
        with open(CREDS_FILE) as f:
            c = json.load(f)
        cid = c.get("client_id")
        secret = c.get("client_secret")
        return (cid, secret) if cid and secret else None
    except (OSError, ValueError):
        return None


def configured():
    return credentials() is not None


def _http(req, timeout=10):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


_token = {"value": None, "expires": 0.0}


def _bearer():
    """A cached app token (client-credentials grant, ~1h lifetime)."""
    if _token["value"] and time.time() < _token["expires"]:
        return _token["value"]
    creds = credentials()
    if not creds:
        raise RuntimeError("Spotify API credentials not set up "
                           f"(missing {CREDS_FILE} — see install.sh)")
    basic = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
    req = urllib.request.Request(
        ACCOUNTS, data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + basic,
                 "Content-Type": "application/x-www-form-urlencoded"})
    tok = _http(req)
    _token["value"] = tok["access_token"]
    _token["expires"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    return _token["value"]


def _get(path):
    req = urllib.request.Request(
        API + path, headers={"Authorization": "Bearer " + _bearer()})
    return _http(req)


def _pick_image(images):
    """Smallest image still >=200px — profile covers render at 176px on the
    box and as thumbnails in the PWA; no point hauling the 640px one."""
    best = None
    for im in images or []:
        url, w = im.get("url"), im.get("width")
        if not url:
            continue
        if best is None or (w or 0) >= 200 and (w or 9999) < best[0]:
            best = (w or 9999, url)
    return best[1] if best else None


def user_playlists(user, limit=100):
    """The PUBLIC playlists on a profile -> [{name, target, image}], the
    order the profile shows them in. Raises urllib.error.HTTPError (404 =
    no such user), OSError (offline) or RuntimeError (not configured)."""
    quoted = urllib.parse.quote(str(user), safe="")
    out, path = [], f"/users/{quoted}/playlists?limit=50"
    while path and len(out) < limit:
        page = _get(path)
        for it in page.get("items") or []:
            if not it or not it.get("id"):  # deleted playlists come as null
                continue
            out.append({
                "name": str(it.get("name") or "Playlist"),
                "target": f"https://open.spotify.com/playlist/{it['id']}",
                "image": _pick_image(it.get("images")),
            })
        nxt = page.get("next")
        path = nxt[len(API):] if nxt and nxt.startswith(API) else None
    return out[:limit]
