#!/usr/bin/env python3
"""The Storytel daemon endpoints: privileged, and secret-tight.

The three POSTs (credentials, shelf, logout) reveal or change an account
and must be behind the token — which they are FOR FREE, by default-deny,
since SAFE lists neither. The one GET is open to the whole LAN by
structural necessity, so it must carry no email, no jwt, no password.
This pins both, plus that saving credentials never echoes the password
back and writes the file 0600."""
import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "api-token")
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
CREDS = os.path.join(TMP, "storytel.json")
os.environ["VIBB_STORYTEL_CREDS"] = CREDS
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import token, storytel  # noqa: E402

TOKEN = token.ensure()

# fake the network: a login that works, a shelf with one series
storytel._request = lambda url, method="GET", headers=None, data=None, \
    timeout=15, follow=True: (
        (200, {}, json.dumps({"accountInfo": {"jwt": "J"}}).encode())
        if "login.action" in url else
        (200, {}, json.dumps({"items": {"a": {"model": {
            "id": "111", "title": "En", "kidsBook": True,
            "seriesInfo": {"id": "26175", "name": "Kokosbananas",
                           "orderInSeries": 1},
            "formats": [{"type": "abook", "id": "111",
                         "durationInMilliseconds": 700000,
                         "isLockedContent": False}]}}}}).encode())
        if url.endswith("/bookshelf") else (404, {}, b""))

srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, method="POST", body=None, tok=None):
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("X-Vibb-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 1. all three POSTs are privileged — refused without the token, by
#    default-deny (nothing was added to SAFE)
for path, b in (("/storytel/credentials", {"email": "k@vibb.me",
                                            "password": "pw"}),
                ("/storytel/shelf", {}),
                ("/storytel/logout", {})):
    st, _ = call(path, body=b)
    assert st == 401, f"{path} must be privileged, got {st}"
assert not storytel.configured(), "a refused call must not have saved anything"
print("1. all three storytel POSTs are privileged (default-deny) OK")

# 2. saving credentials works with the token, validates via a probe login,
#    and NEVER echoes the password back
st, body = call("/storytel/credentials",
                body={"email": "k@vibb.me", "password": "sekret"}, tok=TOKEN)
assert st == 200, (st, body)
assert "sekret" not in body, "the password must never appear in a response"
assert json.loads(body).get("configured") is True
print("2. credentials save behind the token, and the password is never echoed OK")

# 3. the file is 0600 and holds the password, but no endpoint returns it
assert stat.S_IMODE(os.stat(CREDS).st_mode) == 0o600, "creds must be 0600"
st, body = call("/storytel/status", method="GET")     # open to the LAN
assert st == 200, body
data = json.loads(body)
assert data == {"configured": True, "queued": 0, "sync": True}, data
assert "k@vibb.me" not in body and "sekret" not in body, \
    "the open GET must leak no email and no password"
print("3. creds file is 0600, and the open status leaks no PII OK")

# 4. the shelf picker returns grouped series with the token
st, body = call("/storytel/shelf", tok=TOKEN)
assert st == 200, (st, body)
series = json.loads(body)["series"]
assert series[0]["target"] == "storytel:series:26175"
assert series[0]["books"][0]["consumable_id"] == "111"
# the picker must distinguish 'added to the library' from 'downloaded' —
# a series added but not yet downloaded must not read as on the box
assert series[0]["in_library"] is False, "not added to the library here"
assert series[0]["downloaded"] == 0, "nothing downloaded"
print("4. the shelf picker groups the account's books into series OK")

# 5. logout clears the account, and a bad password is refused cleanly
st, body = call("/storytel/logout", tok=TOKEN)
assert st == 200 and json.loads(body)["configured"] is False
assert not storytel.configured()
storytel._request = lambda *a, **k: (403, {}, b"nope")   # login now fails
st, body = call("/storytel/credentials",
                body={"email": "x@y.no", "password": "wrong"}, tok=TOKEN)
assert st == 401, (st, body)
assert not storytel.configured(), "a refused login must not leave creds behind"
print("5. logout clears the account, a bad login is refused and forgotten OK")

print("\nSTORYTEL ROUTES OK — privileged by default-deny, 0600 at rest, and "
      "the one open endpoint says nothing personal.")
