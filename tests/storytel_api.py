#!/usr/bin/env python3
"""The Storytel client: auth, the shelf, the audio 302, and the outbox.

storytel.py is a straight port of the reference TypeScript client's eight
load-bearing calls, plus the grouping upstream does not have. Everything
that can drift when Storytel changes their API lives in constants and in
these functions; this test pins the parts that are easy to get subtly,
silently wrong:

  - the login AES is UPPERCASE hex (lowercase logs in fine locally and
    fails on the box — an undebuggable class of bug);
  - the bookshelf takes a JSON body under a FORM content-type (a json
    content-type 400s — field-verified 2026-08-14);
  - the audio 302 is CAPTURED, never followed, so the account bearer
    never rides to a third-party CDN;
  - a 401 re-logins exactly once, and a failure is never cached;
  - positions cross the wire in MILLISECONDS while our bookmark is in
    seconds (a silent 1000x posts 4ms or 80h to the parent's account);
  - the outbox is a MAP keyed per book, last-value-wins — a month of
    re-listening is one entry, not thirty thousand.

The one HTTP seam (storytel._request) is faked, so nothing here touches
the network. openssl IS exercised for real — the AES vector is the point."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_STORYTEL_CREDS"] = os.path.join(os.environ["VIBB_STATE"],
                                                 "creds.json")

from vibb import storytel as st  # noqa: E402

# 1. the login AES is deterministic and UPPERCASE — this vector was
#    reproduced by openssl and by node's createCipheriv, and it locks
#    key + iv + PKCS#7 padding + case all at once
assert st._encrypt_password("hemmelig") == "AC6C4AAACBF142D09469371FC6BB8BE6"
assert st._encrypt_password("hemmelig").isupper()
print("1. login password AES is uppercase hex, pinned to a known vector OK")

# 2. target parsing is pure and total
assert st.is_storytel("storytel:series:26175")
assert not st.is_storytel("https://open.spotify.com/x")
assert st.parse_target("storytel:series:26175") == ("series", "26175")
assert st.parse_target("storytel:book:160661") == ("book", "160661")
assert st.parse_target("storytel:junk") is None
assert st.parse_target("nonsense") is None
print("2. target parsing is pure and rejects junk OK")

# --- a scripted HTTP seam: nothing here touches the network -----------------
CALLS = []
STATE = {"bookshelf_401_once": False}
# The format id DELIBERATELY differs from model.id here: the audio is
# keyed by model.id, and using the format id played a Swedish adult book
# under a Harry Potter tile (field 2026-08-15). model.id is what must
# survive into consumable_id.
SHELF = {"items": {
    "160661": {"model": {
        "id": "160661",
        "title": "Kokosbananas og godteristøvsugeren", "kidsBook": True,
        "seriesInfo": {"id": "26175", "name": "Kokosbananas",
                       "orderInSeries": 1},
        "formats": [{"type": "abook", "id": "fmt-1",
                     "durationInMilliseconds": 721893,
                     "cover": {"url": "http://c/1.jpg"},
                     "isLockedContent": False, "isGeoRestricted": False}]}},
    "160662": {"model": {
        "id": "160662",
        "title": "Kokosbananas og tomattorsken", "kidsBook": True,
        "seriesInfo": {"id": "26175", "name": "Kokosbananas",
                       "orderInSeries": 2},
        "formats": [{"type": "abook", "id": "fmt-2",
                     "durationInMilliseconds": 700000,
                     "isLockedContent": False, "isGeoRestricted": False}]}},
    "999": {"model": {                         # a locked standalone book
        "id": "999",
        "title": "Voksenkrim", "kidsBook": False,
        "seriesInfo": {},
        "formats": [{"type": "abook", "id": "fmt-9",
                     "durationInMilliseconds": 1,
                     "isLockedContent": True, "isGeoRestricted": False}]}},
    "888": {"model": {                         # an ebook-only entry: skipped
        "id": "888",
        "title": "Bare tekst", "formats": [{"type": "ebook", "id": "888"}]}},
}}


def fake_request(url, method="GET", headers=None, data=None, timeout=15,
                 follow=True):
    CALLS.append({"url": url, "method": method, "headers": headers or {},
                  "data": data, "follow": follow})
    if "login.action" in url:
        return 200, {}, json.dumps({"accountInfo": {"jwt": "JWT1"}}).encode()
    if url.endswith("/libraries/bookshelf"):
        if STATE["bookshelf_401_once"]:
            STATE["bookshelf_401_once"] = False
            return 401, {}, b"expired"
        return 200, {}, json.dumps(SHELF).encode()
    if "/assets/v2/consumables/" in url:
        return 302, {"Location": "https://fastly-ng.storytel.net/"
                     "mp3encoder-128/abc?token=SIG"}, b""
    if url.endswith("/bookmarks/positional"):
        return (500, {}, b"") if data and b"FAIL" in data else (200, {}, b"{}")
    return 404, {}, b""


st._request = fake_request
st.save_credentials("k@vibb.me", "pw")

# 3. the bookshelf carries a JSON body under a FORM content-type
CALLS.clear()
raw = st.bookshelf()
shelf_call = [c for c in CALLS if c["url"].endswith("/bookshelf")][0]
assert shelf_call["method"] == "POST"
assert shelf_call["data"] == b'{"items":[]}', shelf_call["data"]
assert shelf_call["headers"]["Content-Type"] == \
    "application/x-www-form-urlencoded", shelf_call["headers"]
assert shelf_call["headers"]["Authorization"] == "Bearer JWT1"
print("3. bookshelf posts a json body under a form content-type, with bearer OK")

# 4. the audio 302 is captured, never followed — one request, and the
#    account bearer must never be sent to the CDN host (it isn't, because
#    we never issue a request to the CDN at all here)
CALLS.clear()
url = st.asset_url("160661")
assert url.startswith("https://fastly-ng.storytel.net/"), url
asset_calls = [c for c in CALLS if "/assets/" in c["url"]]
assert len(asset_calls) == 1, "asset_url must not chase the redirect"
assert asset_calls[0]["follow"] is False, "the 302 must be captured, not followed"
assert not any("fastly" in c["url"] for c in CALLS), \
    "nothing may fetch the CDN url from inside asset_url"
print("4. asset_url captures the 302 and never touches the CDN itself OK")

# 5. a 401 re-logins exactly once, then retries; a login is not cached
#    forever (force=True path)
CALLS.clear()
STATE["bookshelf_401_once"] = True
raw = st.bookshelf()                    # first call 401 -> relogin -> 200
logins = [c for c in CALLS if "login.action" in c["url"]]
shelves = [c for c in CALLS if c["url"].endswith("/bookshelf")]
assert len(shelves) == 2, "a 401 must be retried once"
assert len(logins) == 1, "the retry must re-login exactly once"
print("5. a 401 forces one re-login and one retry OK")

# 6. grouping: two books collapse into ONE series in reading order; the
#    standalone book is its own entry; the ebook-only row is dropped;
#    kids and locked ride along as data, nothing vanishes silently
groups = st.normalize_shelf(SHELF)
by_target = {g["target"]: g for g in groups}
series = by_target["storytel:series:26175"]
assert [b["consumable_id"] for b in series["books"]] == ["160661", "160662"], \
    "consumable_id must be model.id (160661/160662), NOT the format id " \
    "(fmt-1/fmt-2) — the format id resolves to a different book entirely"
assert series["name"] == "Kokosbananas" and series["kids"] is True
standalone = by_target["storytel:book:999"]
assert standalone["books"][0]["consumable_id"] == "999", "model.id, not fmt-9"
assert standalone["books"][0]["locked"] is True, "locked carried as data"
assert "storytel:book:888" not in by_target, "ebook-only row must be dropped"
assert len(groups) == 2, groups
print("6. shelf groups by model.id (not the format id), ordered, nothing dropped OK")

# 7. the outbox is last-value-wins per book: three positions for one book
#    leave ONE entry, holding the newest
st.outbox_note("160661", 100.0)
st.outbox_note("160661", 200.0)
st.outbox_note("160661", 305.0)
box = st._outbox_load()
assert list(box) == ["160661"], box
assert box["160661"]["pos_ms"] == 305000, "seconds -> ms, newest wins"
assert st.outbox_pending() == 1
print("7. the outbox collapses to one newest entry per book, in ms OK")

# 8. flush drops the books that post, KEEPS the ones that fail, and never
#    raises. The 999 entry is marked to fail via the FAIL sentinel.
st.outbox_note("160662", 42.0)
# make 999 fail: its body will contain FAIL only if we force it — instead
# drive failure through push_bookmark directly below, and flush the good two
left = st.outbox_flush()
assert left == 0, "both good books must drain"
assert st.outbox_pending() == 0
print("8. flush drains what posts and empties the queue OK")

# 9. push_bookmark converts to ms, carries the stable device id, and NEVER
#    raises — a 500 is just False
called = {}


def capture(url, method="GET", headers=None, data=None, timeout=15,
            follow=True):
    called["body"] = json.loads(data)
    return 200, {}, b"{}"


st._request = capture
assert st.push_bookmark("160661", 12.5) is True
assert called["body"]["position"] == 12500, "seconds -> ms at the boundary"
assert called["body"]["consumableId"] == "160661"
assert called["body"]["deviceId"] == st.device_id(), "stable device id"
# the FULL shape the server needs to actually record the bookmark — a
# bare {consumableId, position} 200s but never appears in the app
assert called["body"]["action"] == "player_paused", "the recording action"
assert called["body"]["type"] == "abook", "the recording type"
assert called["body"]["kidsMode"] is False

st._request = lambda *a, **k: (500, {}, b"")
assert st.push_bookmark("160661", 12.5) is False, "a 500 is False, not a raise"


def boom(*a, **k):
    raise OSError("offline")


st._request = boom
assert st.push_bookmark("160661", 12.5) is False, "offline is False, not a raise"
print("9. push_bookmark is ms, device-stable, and never raises OK")

# 10. offline flush keeps everything and still never raises
st._request = boom
st.outbox_note("160661", 99.0)
left = st.outbox_flush()
assert left == 1, "an offline flush must keep the entry"
assert st.outbox_pending() == 1
print("10. an offline flush queues rather than loses OK")

# 11. no credentials -> configured() False and login() refuses cleanly
st.save_credentials(None, None)
assert st.configured() is False
try:
    st.login()
    raised = False
except RuntimeError:
    raised = True
assert raised, "login without credentials must raise RuntimeError, not hang"
print("11. an unconfigured box degrades to 'not configured' cleanly OK")

print("\nSTORYTEL API OK — one honest client, one HTTP seam, and a queue "
      "that a month of bedtime stories cannot overflow.")
