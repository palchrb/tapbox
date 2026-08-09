"""The renderer axis + the tapbox-sonos sidecar client (stdlib).

`output` (local|bt) keeps meaning "which ALSA pcm OUR player writes to".
`renderer` is the orthogonal axis: box = we make sound, sonos:<uid> = a
speaker renders and the box is the controller. Kept in its own file so
btwatchd/expand/UI never grow a third `output` value — putting "sonos"
into OUTPUT_PCMS poisons player.py's pcm read and output.py's fallback
(architect review 2026-08-08).

Client error shapes are deliberately distinct (QA review 2026-08-09):
sidecar down (ECONNREFUSED) is NOT speaker down (sidecar answers with
transport UNREACHABLE) is NOT "nothing playing" — collapsing them into
{} powers the box off mid-episode.
"""

import json
import os
import urllib.error
import urllib.request

from tapbox.paths import STATE_DIR

RENDERER_FILE = os.path.join(STATE_DIR, "renderer.json")
SONOS_API = os.environ.get("TAPBOX_SONOS_API", "http://127.0.0.1:3681")


def read():
    """{'renderer': 'box'|'sonos', 'uid':..., 'name':...}. Garbage or a
    missing file is 'box' — the value a fresh install must have."""
    try:
        with open(RENDERER_FILE) as f:
            d = json.load(f)
        if d.get("renderer") == "sonos" and d.get("uid"):
            return d
    except (OSError, ValueError):
        pass
    return {"renderer": "box"}


def write(renderer, uid=None, name=None):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = RENDERER_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"renderer": renderer, "uid": uid, "name": name}, f)
    os.replace(tmp, RENDERER_FILE)


def is_sonos():
    return read()["renderer"] == "sonos"


class SidecarDown(OSError):
    """tapbox-sonos itself is unreachable — freeze, never zero, and never
    confuse with the SPEAKER being unreachable (that comes back as a 200
    with transport UNREACHABLE)."""


def get(path, timeout=3):
    try:
        with urllib.request.urlopen(SONOS_API + path, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except (OSError, ValueError) as e:
        raise SidecarDown(str(e))


def post(path, body=None, timeout=15):
    """POST to the sidecar. Returns (status_code, dict) — HTTP errors are
    RETURNED, not raised: 409/502/404 carry policy meaning upstream."""
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(SONOS_API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except OSError as e:
        raise SidecarDown(str(e))
