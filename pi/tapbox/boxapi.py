"""tapboxd HTTP client — for the thin daemons/CLIs (rfid, buttons, idle, ui)."""

import json
import os
import urllib.request

BASE = os.environ.get("TAPBOX_DAEMON", "http://127.0.0.1:3679")


def _request(method, path, body, timeout):
    data = None if body is None and method == "GET" \
        else json.dumps(body if body is not None else {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(path, timeout=10):
    return _request("GET", path, None, timeout)


def post(path, body=None, timeout=15):
    return _request("POST", path, body, timeout)


def put(path, body, timeout=10):
    return _request("PUT", path, body, timeout)
