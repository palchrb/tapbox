#!/usr/bin/env python3
"""Gate PWA media upload — the box's own-content path.

Playback of a local folder already works (content.py:830 handles
mp3/m4a/m4b/ogg/opus/flac/wav with per-file bookmarks and cover.jpg as
art), so getting files ONTO the box is the whole feature. Books run
150-400MB, which drives every rule here:

- the body is STREAMED to disk; buffering a 300MB upload would take a
  512MB box down;
- a full SD card breaks playback, the sweep and the bookmarks — not just
  the upload — so we refuse below a free-space floor instead of filling
  it;
- uploads name their own files, so the name is the security boundary
  (no traversal, known extensions only);
- and the route takes octet-stream, which keeps the CSRF guard intact:
  an HTML form can send multipart but NOT octet-stream, so a drive-by
  page still can't reach it."""
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
MEDIA = os.path.join(TMP, "media")
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_MEDIA"] = MEDIA
os.environ["TAPBOX_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["TAPBOX_TOKEN_FILE"] = os.path.join(TMP, "api-token")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from tapbox import token  # noqa: E402

TOKEN = token.ensure()
srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def upload(coll, name, data, tok=TOKEN, ctype="application/octet-stream"):
    url = f"{BASE}/media/upload?collection={coll}&name={name}"
    req = urllib.request.Request(url, data=data, method="POST")
    if ctype:
        req.add_header("Content-Type", ctype)
    if tok:
        req.add_header("X-TapBox-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def api(path, body=None, method="GET"):
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-TapBox-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# 1. a normal upload lands on disk, byte-for-byte
audio = b"ID3" + os.urandom(300_000)
st, r = upload("Ronja", "01-kapittel.mp3", audio)
assert st == 200, (st, r)
dest = os.path.join(MEDIA, "Ronja", "01-kapittel.mp3")
assert open(dest, "rb").read() == audio, "the file must survive intact"
print(f"1. upload lands intact ({len(audio) // 1000} kB) OK")

# 1b. no .part file is left behind
assert not [f for f in os.listdir(os.path.dirname(dest))
            if f.endswith(".part")], os.listdir(os.path.dirname(dest))
print("1b. no leftover .part after a successful upload OK")

# 2. THE POINT: the box can already PLAY what was uploaded — the folder
#    path expands to a playable list with per-file ids for bookmarks
from tapbox import content  # noqa: E402

upload("Ronja", "02-kapittel.mp3", b"ID3" + os.urandom(1000))
entries = content.expand_entries(os.path.join(MEDIA, "Ronja"))
assert [e["id"] for e in entries] == ["01-kapittel.mp3", "02-kapittel.mp3"], \
    entries
assert all(os.path.exists(e["url"]) for e in entries), entries
print("2. uploaded folder expands to a playable, bookmarkable list OK")

# 3. TRAVERSAL: a hostile name must not escape the media dir
for bad in ("../../etc/passwd.mp3", "..%2Fescape.mp3", "/abs/path.mp3"):
    st, r = upload("Ronja", bad, b"x" * 10)
    if st == 200:
        assert os.path.dirname(os.path.realpath(
            os.path.join(MEDIA, "Ronja", r["name"]))) == \
            os.path.realpath(os.path.join(MEDIA, "Ronja")), r
print("3. path traversal in the filename is neutralized OK")
# they landed safely INSIDE the collection (that's the point) — tidy up
# so the listing assertions below are about the real uploads
for f in os.listdir(os.path.join(MEDIA, "Ronja")):
    if f not in ("01-kapittel.mp3", "02-kapittel.mp3"):
        fp = os.path.join(MEDIA, "Ronja", f)
        if os.path.isdir(fp):     # .art/ — same shape /media/delete handles
            for a in os.listdir(fp):
                os.remove(os.path.join(fp, a))
            os.rmdir(fp)
        else:
            os.remove(fp)

# 3b. unknown extensions are refused (no scripts/binaries onto the box)
for bad in ("evil.sh", "evil.py", "noext"):
    st, _ = upload("Ronja", bad, b"x" * 10)
    assert st == 400, f"{bad} must be refused, got {st}"
print("3b. non-audio extensions refused OK")

# 4. AUTH: uploading is privileged — an unlinked phone cannot write to
#    the box's disk
st, r = upload("Ronja", "sneak.mp3", b"x" * 10, tok=None)
assert st == 401, (st, r)
assert not os.path.exists(os.path.join(MEDIA, "Ronja", "sneak.mp3"))
print("4. upload requires the box token OK")

# 5. CSRF: the route accepts octet-stream (a form can't send that), but
#    the form-reachable content types must still be refused — otherwise
#    a drive-by page could write files to the box
for ctype in ("multipart/form-data", "application/x-www-form-urlencoded",
              "text/plain"):
    st, _ = upload("Ronja", "csrf.mp3", b"x" * 10, ctype=ctype)
    assert st == 415, f"{ctype} must be refused, got {st}"
assert not os.path.exists(os.path.join(MEDIA, "Ronja", "csrf.mp3"))
print("5. form-reachable content types refused (CSRF guard holds) OK")

# 6. DISK FLOOR: refusing beats filling the card — a full card breaks
#    playback and bookmarks, not just the upload
real_free = daemon._free_bytes
daemon._free_bytes = lambda p: daemon.MEDIA_FREE_FLOOR + 1000
st, r = upload("Ronja", "toobig.mp3", b"x" * 50_000)
assert st == 507, (st, r)
assert not os.path.exists(os.path.join(MEDIA, "Ronja", "toobig.mp3"))
daemon._free_bytes = real_free
print("6. upload refused when it would cross the free-space floor OK")

# 7. listing + delete, so the PWA can show and tidy
st, r = api("/media")
coll = next(c for c in r["collections"] if c["name"] == "Ronja")
assert coll["files"] == ["01-kapittel.mp3", "02-kapittel.mp3"], coll
assert coll["bytes"] > 300_000 and "free" in r
print("7. /media lists collections, files and free space OK")

st, _ = api("/media/delete", {"collection": "Ronja",
                             "name": "02-kapittel.mp3"}, "POST")
assert st == 200 and not os.path.exists(
    os.path.join(MEDIA, "Ronja", "02-kapittel.mp3"))
st, _ = api("/media/delete", {"collection": "Ronja"}, "POST")
assert st == 200 and not os.path.exists(os.path.join(MEDIA, "Ronja"))
print("8. delete removes a single file and a whole collection OK")

# 9. MEDIA_DIR must live OUTSIDE the cache — prune_cache would happily
#    delete a 300MB audiobook nobody has another copy of
from tapbox.paths import CACHE_DIR, MEDIA_DIR  # noqa: E402

assert not os.path.realpath(MEDIA_DIR).startswith(
    os.path.realpath(CACHE_DIR) + os.sep), (MEDIA_DIR, CACHE_DIR)
print("9. MEDIA_DIR is outside CACHE_DIR (survives prune_cache) OK")

srv.shutdown()
print("\nall media_upload checks passed")
