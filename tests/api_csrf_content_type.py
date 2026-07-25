#!/usr/bin/env python3
"""Gate the cross-origin CSRF guard: every state-changing request must
carry `Content-Type: application/json`.

The hole this closes was LIVE (QA review 2026-07-25), not theoretical:
do_POST parsed the body with json.loads and, on ValueError, continued
with `body = {}`. So every endpoint that needs no body —
/system/shutdown, /wifi/reconnect, /bt/scan, /spotify/logout, /stop —
could be fired by a plain auto-submitting <form> on ANY page someone on
the LAN opened. A form can only send form-urlencoded / text/plain /
multipart, so requiring JSON makes the whole class unreachable: anything
else forces a CORS preflight this server never grants.

Also pins the two assumptions the guard rests on: no Access-Control-*
headers, and no do_OPTIONS. Adding either would silently undo it."""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_LIBRARY"] = os.path.join(TMP, "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

FIRED = []
daemon.shutdown = lambda restart=False: (FIRED.append("shutdown"),
                                         {"ok": True})[1]
daemon.ORCH.command = lambda a: (FIRED.append(a), {"routed": "x"})[1]

srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def post(path, ctype, data=b"", method="POST"):
    """Returns (status, body_text). Errors are statuses too, not raises."""
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


# 1. THE ATTACK: a plain cross-origin <form> POST (what a browser sends
#    without any preflight) must be refused — and must NOT power off the
#    box. This exact request used to reach shutdown().
st, body, _ = post("/system/shutdown", "application/x-www-form-urlencoded",
                   b"a=1")
assert st == 415, f"form-encoded POST must be refused, got {st}"
assert FIRED == [], f"the box must NOT have acted on it: {FIRED}"
print("1. cross-origin form POST to /system/shutdown: 415, never ran OK")

# 1b. the text/plain smuggling variant (valid JSON in a form body) too
st, _, _ = post("/system/shutdown", "text/plain", b'{"restart": false}')
assert st == 415 and FIRED == [], (st, FIRED)
print("1b. text/plain JSON smuggling: 415, never ran OK")

# 1c. no Content-Type at all
st, _, _ = post("/system/shutdown", None, b"")
assert st == 415 and FIRED == [], (st, FIRED)
print("1c. missing Content-Type: 415, never ran OK")

# 2. the legitimate call still works (the header every internal caller
#    already sends: boxapi.py, app.js, play.sh)
st, _, _ = post("/system/shutdown", "application/json", b"{}")
assert st == 200 and FIRED == ["shutdown"], (st, FIRED)
print("2. proper application/json POST still works OK")

# 2b. a charset parameter must not break it (browsers/clients add it)
FIRED.clear()
st, _, _ = post("/playpause", "application/json; charset=utf-8", b"{}")
assert st == 200 and FIRED == ["playpause"], (st, FIRED)
print("2b. 'application/json; charset=utf-8' accepted OK")

# 3. PUT is gated the same way (PUT /library replaces the whole library)
st, _, _ = post("/library", "text/plain", b'{"sections": []}', method="PUT")
assert st == 415, f"PUT must be gated too, got {st}"
print("3. PUT /library with a non-JSON content type: 415 OK")

# 4. a LARGE rejected body is drained, so the client reads the 415
#    instead of a connection reset (an unread body makes the close an RST)
st, _, _ = post("/library/section-logo", "text/plain", b"x" * 40000)
assert st == 415, f"large rejected body must still return 415, got {st}"
print("4. large rejected body: client gets the 415, no RST OK")

# 5. GET is untouched — the PWA's own page, app.js, icons and <img>
#    artwork loads cannot carry a custom content type. The guard MUST
#    NOT apply to reads.
st, _, _ = post("/status", None, method="GET")
assert st == 200, f"GET /status must stay open, got {st}"
print("5. GET requests unaffected (static + artwork keep working) OK")

# 6. THE ASSUMPTIONS the guard rests on. If either of these ever
#    changes, the CSRF protection is silently gone.
st, _, hdrs = post("/status", None, method="GET")
assert not [h for h in hdrs if h.lower().startswith("access-control")], \
    f"no CORS headers may be sent — that would re-open cross-origin: {hdrs}"
st, _, _ = post("/status", None, method="OPTIONS")
assert st != 200, "do_OPTIONS must not answer 200 (would allow preflight)"
print("6. no Access-Control-* headers, no OPTIONS handler OK")

srv.shutdown()
print("\nall api_csrf_content_type checks passed")
