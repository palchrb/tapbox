#!/usr/bin/env python3
"""Gate the privileged-endpoint gate itself (SECURITY.md Model A+B).

The design is DEFAULT DENY: SAFE lists what works without the box token,
everything else needs it. These tests pin the properties that make that
claim true — especially that a forgotten or newly added endpoint fails
CLOSED, and that the playback controls (the "Hey Siri, pause Vibb"
shortcut) keep working with no setup at all."""
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
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "api-token")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import token  # noqa: E402

TOKEN = token.ensure()
FIRED = []
daemon.shutdown = lambda restart=False: (FIRED.append("shutdown"),
                                         {"ok": True})[1]
daemon.ORCH.command = lambda a: (FIRED.append(a), {"routed": "x"})[1]
daemon.ORCH.play = lambda *a, **k: (FIRED.append(("play",) + a[:1]),
                                    {"source": "x"})[1]
daemon.set_wifi = lambda enabled: (FIRED.append("set_wifi"), {"ok": True})[1]

srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, method="POST", body=None, tok=None, ctype="application/json"):
    data = json.dumps(body if body is not None else {}).encode() \
        if method != "GET" else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    if tok:
        req.add_header("X-Vibb-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# 1. privileged without a token: refused, and the action NEVER ran
for path in ("/system/shutdown", "/system/wifi", "/wifi/connect",
             "/bt/scan", "/bt/lost", "/output", "/spotify/logout"):
    st, body = call(path)
    assert st == 401, f"{path} must be privileged, got {st}"
    assert json.loads(body)["code"] == "token_required", body
assert FIRED == [], f"no privileged action may have run: {FIRED}"
print("1. privileged endpoints without a token: 401, nothing ran OK")

# 2. with the real token they work
st, _ = call("/system/shutdown", tok=TOKEN)
assert st == 200 and FIRED == ["shutdown"], (st, FIRED)
# the human-typed form of the same token works too
FIRED.clear()
st, _ = call("/system/shutdown", tok=token.grouped(TOKEN).lower())
assert st == 200 and FIRED == ["shutdown"], (st, FIRED)
print("2. privileged endpoints with the token (any typed form): 200 OK")

# 3. a WRONG token is distinguishable from a missing one, so the PWA can
#    say "re-link" instead of "link"
FIRED.clear()
st, body = call("/system/shutdown", tok="ZZZZZZZZZZZZZZZZ")
assert st == 401 and json.loads(body)["code"] == "token_invalid", body
assert FIRED == []
print("3. wrong token: 401 token_invalid (distinct from token_required) OK")

# 4. THE SHORTCUT MUST NOT REGRESS: playback works with no token at all.
#    (Some carry their own body validation — a 400 still proves the gate
#    let the request through, which is what's under test here.)
for path, body in (("/playpause", None), ("/next", None), ("/prev", None),
                   ("/pause", None), ("/stop", None),
                   ("/shuffle", {"enabled": True}),
                   ("/volume", {"delta": 5}), ("/seek", {"delta": 30})):
    st, _ = call(path, body=body)
    assert st != 401, f"{path} must stay open (Siri shortcut), got {st}"
print("4. playback controls still work with no token (Siri shortcut) OK")

# 5. GET stays open — static files and <img> artwork loads cannot carry
#    a custom header, so this is structural
st, _ = call("/status", method="GET")
assert st == 200, st
print("5. GET /status open (static + artwork keep loading) OK")

# 6. DEFAULT DENY on an UNKNOWN path: a route nobody classified is
#    privileged, not open. This is the property that protects endpoints
#    added years from now.
st, body = call("/some/future/endpoint")
assert st == 401, f"an unclassified path must fail closed, got {st}"
print("6. unknown POST path is privileged by default OK")

# 7. ...and an unknown METHOD too (a future do_DELETE is gated the day
#    it is written, without anyone remembering this file)
st, _ = call("/status", method="DELETE")
assert st != 200, "an unknown method must not be treated as safe"
print("7. unknown method is not SAFE-listed OK")

# 8. path normalization can't smuggle a privileged path past the gate
FIRED.clear()  # step 4's playback calls are legitimately in here
for variant in ("/system/shutdown/", "/system/shutdown?x=1"):
    st, _ = call(variant)
    assert st == 401, f"{variant} must still be privileged, got {st}"
assert FIRED == []
print("8. trailing slash / query string don't bypass the gate OK")

# 9. THE /play SPLIT: a library id stays open (RFID cards, buttons), a
#    RAW target needs the token — it is the one open endpoint that could
#    put new, uncurated audio in a kid's room.
FIRED.clear()
st, _ = call("/play", body={"target": "https://evil.example/x.mp3"})
assert st == 401, f"a raw target must be privileged, got {st}"
assert FIRED == [], "the box must not have played it"
st, _ = call("/play", body={"target": "https://ok.example/x.mp3"}, tok=TOKEN)
assert st == 200 and FIRED, "a raw target WITH the token must play"
print("9. /play: raw target privileged, with token allowed OK")

# 9b. the {"id": ...} form is unauthenticated — it can only ever play
#     something a parent already curated (404 here proves it reached the
#     handler and did a library lookup rather than being refused)
st, body = call("/play", body={"id": "no-such-entry"})
assert st == 404, f"the id form must stay open (reached lookup), got {st}"
print("9b. /play with a library id stays open OK")

# 10. ROTATION without a restart: the screen re-links a phone and the old
#     token dies immediately, on the SAME running server
NEW = token.rotate()
FIRED.clear()
st, _ = call("/system/shutdown", tok=TOKEN)
assert st == 401, "the old token must stop working at once"
st, _ = call("/system/shutdown", tok=NEW)
assert st == 200 and FIRED == ["shutdown"], (st, FIRED)
print("10. rotation takes effect with no daemon restart OK")

# 11. a MISSING token file denies privileged access instead of opening
#     it (fail closed — the difference between locked and wide open)
os.remove(token.TOKEN_FILE)
FIRED.clear()
st, _ = call("/system/shutdown", tok=NEW)
assert st == 401 and FIRED == [], (st, FIRED)
st, _ = call("/system/shutdown", tok="")
assert st == 401 and FIRED == [], (st, FIRED)
# ...while playback still works, so the box is never bricked
st, _ = call("/playpause")
assert st == 200
print("11. missing token file: privileged denied, playback still fine OK")

srv.shutdown()
print("\nall api_auth_gate checks passed")
