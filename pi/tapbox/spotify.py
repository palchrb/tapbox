"""go-librespot client — the ONE place that talks to the Spotify daemon."""

import json
import os
import subprocess
import re
import time
import urllib.request

API = os.environ.get("TAPBOX_GO_API", "http://127.0.0.1:3678")
CONFIG = os.environ.get("TAPBOX_GO_CONFIG", "")

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


def _conf_dir():
    return os.path.dirname(CONFIG) if CONFIG else ""


def _ctl(verb):
    try:
        subprocess.run(["systemctl", verb, "go-librespot"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def logged_in_user():
    """The account go-librespot has persisted, read from state.json —
    available even before it has (re)connected, unlike the live /status."""
    try:
        with open(os.path.join(_conf_dir(), "state.json")) as f:
            return (json.load(f).get("credentials") or {}).get("username")
    except (OSError, ValueError):
        return None


def zeroconf_open():
    """True while the box advertises as an OPEN Spotify Connect device that
    any account on the LAN can claim — and, with persist_credentials, whose
    login overwrites ours (last-connector-wins). We close this once logged
    in so a passing phone can't hijack the box."""
    try:
        with open(CONFIG) as f:
            for line in f:
                m = re.match(r"\s*zeroconf_enabled:\s*(true|false)\b", line)
                if m:
                    return m.group(1) == "true"
    except OSError:
        pass
    return False


def _set_zeroconf(enabled):
    """Flip the zeroconf_enabled line in config.yml. In-place truncate-write
    keeps the file's owner (go-librespot runs as the login user, not root).
    Returns True when the file actually changed."""
    want = "true" if enabled else "false"
    try:
        with open(CONFIG) as f:
            src = f.read()
    except OSError:
        return False
    new, n = re.subn(r"(?m)^(\s*zeroconf_enabled:\s*)(?:true|false)\b",
                     lambda m: m.group(1) + want, src)
    if n == 0:  # key absent — prepend it
        new = f"zeroconf_enabled: {want}\n" + src
    if new == src:
        return False
    with open(CONFIG, "w") as f:
        f.write(new)
    return True


def lock():
    """Close the open Connect door once an account is logged in, so nobody
    else can claim the box. No-op when already locked or not yet logged in,
    so it is safe to poll on a timer. Returns True only on the transition."""
    if not _conf_dir() or not zeroconf_open() or not logged_in_user():
        return False
    _set_zeroconf(False)
    _ctl("restart")
    return True


def logout():
    """PWA 'Switch account': forget the login AND re-open the Connect door
    so a DIFFERENT account can claim the box from the Spotify app (same
    wifi). The auto-lock (see lock()) closes the door again as soon as the
    new account is on, so the box is never open longer than necessary."""
    conf_dir = _conf_dir()
    if not conf_dir or not os.path.isdir(conf_dir):
        return {"ok": False, "error": "go-librespot config dir not found"}
    _ctl("stop")
    removed = []
    for name in ("credentials.json", "state.json"):
        try:
            os.remove(os.path.join(conf_dir, name))
            removed.append(name)
        except OSError:
            pass
    _set_zeroconf(True)
    _ctl("start")
    return {"ok": True, "removed": removed, "open": True}


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
