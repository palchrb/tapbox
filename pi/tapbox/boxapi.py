"""tapboxd HTTP client — for the thin daemons/CLIs (rfid, buttons, idle, ui)."""

import json
import os
import urllib.request

from tapbox import token

BASE = os.environ.get("TAPBOX_DAEMON", "http://127.0.0.1:3679")


def _request(method, path, body, timeout):
    data = None if body is None and method == "GET" \
        else json.dumps(body if body is not None else {}).encode()
    # Authenticate as the box itself. A loopback exemption would also
    # have worked — nothing but root runs here, and a proxied request is
    # still distinguishable (a reverse proxy makes the peer 127.0.0.1 but
    # sets X-Forwarded-For, which a direct client cannot fake). We use
    # the token instead simply to keep ONE auth rule rather than a rule
    # plus an exemption; it is not a security necessity. The cost is a
    # real dependency: a screen action that is purely local now needs a
    # readable token file. token.header() is therefore best-effort ({}
    # when unreadable), so callers that only touch SAFE endpoints keep
    # working regardless.
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
