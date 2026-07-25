"""tapboxd HTTP client — for the thin daemons/CLIs (rfid, buttons, idle, ui)."""

import json
import os
import urllib.request

from tapbox import token

BASE = os.environ.get("TAPBOX_DAEMON", "http://127.0.0.1:3679")


def _request(method, path, body, timeout):
    data = None if body is None and method == "GET" \
        else json.dumps(body if body is not None else {}).encode()
    # Authenticate as the box itself. Deliberately a token file rather
    # than a localhost bypass: `docs/remote-access.md` contemplates a
    # Caddy reverse proxy, and the day one lands EVERY request arrives
    # from 127.0.0.1 — a loopback exemption would silently expose the
    # whole privileged surface. token.header() is best-effort ({} when
    # unreadable), so callers that only touch SAFE endpoints keep working
    # even without read access to it.
    headers = {"Content-Type": "application/json"}
    headers.update(token.header())
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(path, timeout=10):
    return _request("GET", path, None, timeout)


def post(path, body=None, timeout=15):
    return _request("POST", path, body, timeout)


def put(path, body, timeout=10):
    return _request("PUT", path, body, timeout)
