"""Storytel client — the ONE place that talks to Storytel's API.

UPSTREAM: storytel-player, tag 1.3.0 (github.com/debba/storytel-player)
  server/storytelApi.ts, server/passwordCrypt.ts
Every function that mirrors one of theirs names it in its first docstring
line. When Storytel breaks something:
    git -C ../storytel-player fetch --tags
    git -C ../storytel-player diff 1.3.0..<new> -- server/storytelApi.ts \
                                                   server/passwordCrypt.ts
Read that diff; each hunk lands in the function below that names it, and
every URL / header / body-quirk it touches lives in the CONSTANTS block
at the top of this file, nowhere else. Bump the tag here. Nothing else
in vibb moves. What upstream does NOT have — seriesInfo, kidsBook,
isLockedContent — is ours, from probing the live API (2026-08-14), and
lives in normalize_shelf() below; expect to diagnose roughly half of
future breaks ourselves.

Credentials (email + password) live in a root-owned 0600 file. The
password is stored RECOVERABLE on purpose: login.action returns a jwt
with no refresh token, so an expired session is re-established by
AES-encrypting the plaintext afresh. Anyone who can read the file is
already root on the box. Without the file everything here degrades to
"not configured" and the rest of the box is untouched — same contract
as vibb/spotify_web.py.

Verified against a live account 2026-08-14:
  - login:   GET www.storytel.com/api/login.action?m=1&uid=<e>&pwd=<AES hex>
  - shelf:   POST api.storytel.net/libraries/bookshelf, body {"items":[]}
             but Content-Type x-www-form-urlencoded (a json CT returns 400)
  - audio:   GET api.storytel.net/assets/v2/consumables/<id>/abook, bearer,
             302 -> a signed fastly CDN mp3. The CDN URL needs NO bearer;
             attaching one would leak an account token to a third party.
             Path is stable per book; only the ?token= is per-request, so
             a resumed download re-mints rather than caches the URL.
  - bookmark:POST api.storytel.net/bookmarks/positional {consumableId,
             position (MILLISECONDS), deviceId}. Our local bookmark is in
             SECONDS — convert at the boundary or post 4ms / 80h positions.
No app impersonation: one honest User-Agent, so a pinned fake version
can never become a scheduled outage.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Self-contained as a bare script, exactly like content.py — the sweep
# runs this as `python3 .../vibb/storytel.py sync ...`, where sys.path[0]
# is the vibb/ dir itself. That means NO `from vibb import <module>`
# anywhere reachable from sync(): it would need the package parent on the
# path AND would let vibb/token.py shadow stdlib `token` (via
# concurrent.futures), which is exactly how a whole series "synced" in
# six seconds and downloaded nothing — the ModuleNotFoundError died into
# _sync_one's discarded stderr (field 2026-08-15). Downloads are inlined
# below; only vibb.paths is imported, with the same fallback content uses.
try:
    from vibb.paths import CACHE_DIR, STATE_DIR
except ImportError:                     # bare subprocess: vibb not importable
    CACHE_DIR = os.environ.get("VIBB_CACHE", "/var/lib/vibb/cache")
    STATE_DIR = os.environ.get("VIBB_STATE", "/var/lib/vibb/state")

# --- CONSTANTS: everything upstream might move lives here --------------------
CREDS_FILE = os.environ.get("VIBB_STORYTEL_CREDS", "/etc/vibb/storytel.json")
LOGIN_URL = "https://www.storytel.com/api/login.action"
API = "https://api.storytel.net"
BOOKSHELF = API + "/libraries/bookshelf"
ASSET = API + "/assets/v2/consumables/{cid}/abook"      # /abook is required
BOOKMARK = API + "/bookmarks/positional"
AES_KEY = b"VQZBJ6TD8M9WBUWT"
AES_IV = b"joiwef08u23j341a"
# One honest agent; if Storytel ever gates on it, this is the one line to
# change (never a pinned fake app version — that is a timed outage).
UA = "vibb/1 (+https://codeberg.org/palchrb/vibb)"
JWT_TTL_S = 3600.0        # login.action gives no expiry; re-login on 401 too
TARGET_RE = re.compile(r"^storytel:(series|book):([A-Za-z0-9_-]+)$")

_SESSION_FILE = os.path.join(STATE_DIR, "storytel-session.json")
_DEVICE_FILE = os.path.join(STATE_DIR, "storytel-device.json")
_OUTBOX_FILE = os.path.join(STATE_DIR, "storytel-outbox.json")
_OUTBOX_MAX = 200         # keep the newest N books; the queue is a MAP, not
#                           a log, so this only ever bites a huge library


def _log(msg):
    # stderr, because the sweep runs this as a subprocess whose STDOUT is
    # the sync protocol channel (same reason content.py logs to stderr).
    print(f"storytel: {msg}", file=sys.stderr, flush=True)


# --- target parsing (no I/O, safe to call at 1 Hz) --------------------------
def is_storytel(target):
    return isinstance(target, str) and target.startswith("storytel:")


def parse_target(target):
    """('series'|'book', id) or None."""
    m = TARGET_RE.match(target or "")
    return (m.group(1), m.group(2)) if m else None


def series_target(series_id):
    return f"storytel:series:{series_id}"


def book_target(consumable_id):
    return f"storytel:book:{consumable_id}"


def cache_dir(target):
    """CACHE_DIR/storytel-<12 hex> for a target — matches cache_key_for in
    content.py, which is what keeps prune_cache from deleting the books."""
    import hashlib
    return os.path.join(CACHE_DIR,
                        "storytel-" + hashlib.sha1(
                            target.encode()).hexdigest()[:12])


# --- credentials, session, device -------------------------------------------
def credentials():
    """(email, password) or None. Swallows everything — a missing or
    unreadable file just means 'not configured'."""
    try:
        with open(CREDS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        email, pw = d.get("email"), d.get("password")
        return (email, pw) if email and pw else None
    except (OSError, ValueError):
        return None


def configured():
    return credentials() is not None


def _write_private(path, obj):
    """0600 from creation (never chmod after — that leaves a window), then
    atomic rename. Same pattern as token._write."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def save_credentials(email, password):
    """Store the account, or clear it when email is falsy. Never logged."""
    if not email:
        for p in (CREDS_FILE, _SESSION_FILE):
            try:
                os.remove(p)
            except OSError:
                pass
        _jwt["value"] = None
        return
    _write_private(CREDS_FILE, {"email": email, "password": password})
    _jwt["value"] = None   # force a fresh login with the new account


def device_id():
    """One stable id per box, generated once. The reference client mints a
    NEW random id on every bookmark write (fastify-common.ts) unless
    DEVICE_ID is set — which would look like hundreds of devices to an
    account with device limits. This is that fix."""
    try:
        with open(_DEVICE_FILE, encoding="utf-8") as f:
            did = json.load(f).get("device_id")
        if did:
            return did
    except (OSError, ValueError):
        pass
    did = str(uuid.uuid4()).upper()
    try:
        _write_private(_DEVICE_FILE, {"device_id": did})
    except OSError:
        pass
    return did


# --- AES for the login password (passwordCrypt.ts encryptPassword) ----------
def _encrypt_password(pw):
    """AES-128-CBC, PKCS#7, hex uppercase. Shelled to openssl: system
    python has no AES, and openssl is already on the box, on macOS and on
    every Pi image. The password goes in over stdin, never the arg list,
    so it never appears in the process table."""
    out = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-K", AES_KEY.hex(),
         "-iv", AES_IV.hex()],
        input=pw.encode(), capture_output=True, check=True).stdout
    return out.hex().upper()


# --- the ONE HTTP seam (tests fake this) ------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Capture a 302 instead of chasing it: we need the Location, and an
    auto re-issue may or may not strip Authorization across hosts by
    python version — never leave a bearer leak to chance."""

    def redirect_request(self, *a, **k):
        return None


_OPENER_NOREDIR = urllib.request.build_opener(_NoRedirect)


def _request(url, method="GET", headers=None, data=None, timeout=15,
             follow=True):
    """(status, headers, body_bytes) for ANY http response, 3xx/4xx/5xx
    included — the caller reads the status. Raises OSError only on a real
    connection failure. This is the single point the tests monkeypatch."""
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"User-Agent": UA, **(headers or {})})
    opener = urllib.request.urlopen if follow else _OPENER_NOREDIR.open
    try:
        with opener(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:      # a response WITH a status
        return e.code, dict(e.headers), e.read()
    # URLError / socket timeout fall through as OSError — "unreachable"


# --- auth (login, getApiBearer) ---------------------------------------------
_jwt = {"value": None, "expires": 0.0}


def login():
    """login.action -> a fresh jwt. Raises OSError when unreachable,
    RuntimeError when unconfigured or refused."""
    creds = credentials()
    if not creds:
        raise RuntimeError("storytel: not configured")
    email, pw = creds
    q = urllib.parse.urlencode({"m": 1, "uid": email,
                                "pwd": _encrypt_password(pw)})
    status, _h, body = _request(f"{LOGIN_URL}?{q}", timeout=20)
    if status != 200:
        raise RuntimeError(f"storytel: login refused ({status})")
    jwt = ((json.loads(body or b"{}").get("accountInfo") or {}).get("jwt"))
    if not jwt:
        raise RuntimeError("storytel: login returned no jwt")
    _jwt["value"], _jwt["expires"] = jwt, time.monotonic() + JWT_TTL_S
    try:
        _write_private(_SESSION_FILE, {"jwt": jwt, "at": time.time()})
    except OSError:
        pass
    return jwt


def _bearer(force=False):
    """A valid jwt, cached. Re-logins past the TTL or on demand (a 401)."""
    if not force and _jwt["value"] and time.monotonic() < _jwt["expires"]:
        return _jwt["value"]
    return login()


def _api(path_or_url, method="GET", headers=None, data=None, timeout=15,
         follow=True):
    """An authenticated call with ONE automatic re-login on a 401. Returns
    (status, headers, body). Raises like _request/login."""
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    for attempt in (0, 1):
        h = {"Authorization": "Bearer " + _bearer(force=bool(attempt)),
             "Accept": "*/*", **(headers or {})}
        status, rh, body = _request(url, method, h, data, timeout, follow)
        if status != 401 or attempt:
            return status, rh, body
    return status, rh, body


# --- reads: RAISE (the "do the thing" contract) -----------------------------
def bookshelf():
    """getBookshelf (fetch only). The raw shelf. Raises when unreachable or
    refused. NOTE the content-type quirk: a JSON body under a FORM
    content-type. A json content-type returns 400 — field-verified, not a
    typo."""
    status, _h, body = _api(
        BOOKSHELF, method="POST", data=b'{"items":[]}',
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        raise OSError(f"storytel: bookshelf {status}")
    return json.loads(body or b"{}")


def asset_url(consumable_id):
    """getAudioStreamUrl. The signed CDN mp3 for one book, from the 302
    Location — NOT followed, so the bearer never rides to the CDN. Raises
    OSError on a locked/geo/absent title (no redirect) or when
    unreachable."""
    status, headers, _b = _api(ASSET.format(cid=consumable_id), follow=False)
    loc = headers.get("Location") or headers.get("location")
    if not loc:
        raise OSError(f"storytel: no audio url for {consumable_id} ({status})")
    return loc


# --- grouping: OURS, upstream has none of this ------------------------------
def _cover(model, fmt):
    c = (fmt.get("cover") or {}).get("url")
    return c or (model.get("cover") or {}).get("url")


def normalize_shelf(raw):
    """Raw bookshelf -> series, each with its books in reading order.

    Pure, no I/O. Groups audiobooks by seriesInfo.id (a standalone book is
    its own one-book 'series' with series_id None). Carries kidsBook and
    the locked/geo flags as DATA — the picker decides what to filter, the
    downloader decides what to skip; neither is dropped silently here."""
    groups = {}     # series_id -> dict; None-keyed handled per-book
    order = []       # preserve first-seen order of series
    for item in (raw.get("items") or {}).values():
        model = item.get("model") or item
        book = None
        for fmt in model.get("formats") or []:
            if fmt.get("type") == "abook":
                book = fmt
                break
        if not book:
            continue                      # ebook-only shelf entry
        si = model.get("seriesInfo") or {}
        sid = si.get("id")
        entry = {
            "consumable_id": book.get("id"),
            "title": model.get("title"),
            "order": si.get("orderInSeries") or 0,
            "duration_ms": book.get("durationInMilliseconds") or 0,
            "cover": _cover(model, book),
            "kids": bool(model.get("kidsBook")),
            "locked": bool(book.get("isLockedContent")),
            "geo": bool(book.get("isGeoRestricted")),
        }
        key = ("series", sid) if sid else ("book", entry["consumable_id"])
        if key not in groups:
            groups[key] = {
                "kind": key[0],
                "series_id": sid,
                "target": (series_target(sid) if sid
                           else book_target(entry["consumable_id"])),
                "name": si.get("name") or model.get("title"),
                "kids": entry["kids"],
                "cover": entry["cover"],
                "books": [],
            }
            order.append(key)
        groups[key]["books"].append(entry)
    out = [groups[k] for k in order]
    for g in out:
        g["books"].sort(key=lambda b: (b["order"], b["title"] or ""))
        g["kids"] = any(b["kids"] for b in g["books"])
    return out


# --- on-disk layout: storytel owns its own cache format ---------------------
# CACHE_DIR/storytel-<hash>/ holds, for one target:
#   shelf.json                 the book list (title/order/duration), written
#                              at sync time so expand needs no network
#   <consumableId>.mp3         a downloaded book (absent until downloaded)
#   <consumableId>.jpg         per-book cover;  cover.jpg = the series face
_SHELF_JSON = "shelf.json"


def write_shelf(target, name, books):
    """Persist the book list for a target so expand_entries reads it with
    NO network — the same discipline as the RSS feed.json cache. `books`
    is the normalize_shelf 'books' list. Best-effort."""
    d = cache_dir(target)
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, _SHELF_JSON + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"target": target, "name": name, "books": books}, f)
        os.replace(tmp, os.path.join(d, _SHELF_JSON))
    except OSError as e:
        _log(f"write_shelf {target}: {e}")


def read_shelf(target):
    """The persisted book list, or None. Local read, never raises."""
    try:
        with open(os.path.join(cache_dir(target), _SHELF_JSON),
                  encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def book_path(target, consumable_id):
    return os.path.join(cache_dir(target), f"{consumable_id}.mp3")


def local_cover(target):
    """The series' cover file if downloaded, else None. Local only — for
    collection_image, which must never touch the network."""
    p = os.path.join(cache_dir(target), "cover.jpg")
    return p if os.path.exists(p) else None


def entries_for(target):
    """target -> [{'url','title','id','image'}] for its DOWNLOADED books.

    Download-only by design: a signed CDN url expires and cannot be
    cached, so a book that is not on disk is OMITTED rather than streamed.
    Reads shelf.json and the local mp3s — ZERO network, so this is safe on
    the playback path (STALE_OK) and offline, exactly like a cached feed.
    Books come back in reading order."""
    shelf = read_shelf(target)
    if not shelf:
        return []
    d = cache_dir(target)
    rows = []
    for b in shelf.get("books") or []:
        cid = b.get("consumable_id")
        mp3 = os.path.join(d, f"{cid}.mp3")
        if not cid or not os.path.exists(mp3):
            continue                       # not downloaded yet -> omit
        jpg = os.path.join(d, f"{cid}.jpg")
        cover = jpg if os.path.exists(jpg) else local_cover(target)
        rows.append({"url": mp3, "title": b.get("title"), "id": str(cid),
                     "image": cover})
    return rows


def newest_book_id(target):
    """The last book (highest orderInSeries) in the persisted shelf, for
    the 'new content' badge. Local read, or None."""
    shelf = read_shelf(target)
    books = (shelf or {}).get("books") or []
    return str(books[-1].get("consumable_id")) if books else None


def downloaded_count(target):
    """How many of a target's books are actually on disk (mp3 present).
    Local read — for the picker, so 'on the box' means downloaded, not
    merely added to the library."""
    d = cache_dir(target)
    try:
        return sum(1 for f in os.listdir(d) if f.endswith(".mp3"))
    except OSError:
        return 0


# --- the sync-out outbox: a MAP keyed per book, never a log -----------------
def _outbox_load():
    try:
        with open(_OUTBOX_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _outbox_save(box):
    if len(box) > _OUTBOX_MAX:            # keep the newest N by update time
        newest = sorted(box.items(), key=lambda kv: kv[1].get("at", 0.0),
                        reverse=True)[:_OUTBOX_MAX]
        box = dict(newest)
    try:
        _write_private(_OUTBOX_FILE, box)
    except OSError:
        pass


def outbox_note(consumable_id, position_s):
    """Queue a position to mirror out. Last-value-wins per book — a child
    re-hearing one book for a month adds ONE entry, overwritten, never a
    stream of ticks. The local bookmark is already the source of truth;
    this only ever leaves the box when online."""
    box = _outbox_load()
    box[str(consumable_id)] = {"pos_ms": int(position_s * 1000),
                               "at": time.time()}
    _outbox_save(box)


def outbox_pending():
    """How many books are waiting to sync (for GET /storytel/status)."""
    return len(_outbox_load())


def push_bookmark(consumable_id, position_s):
    """updateBookmarkPositional. Fire-and-forget: returns True on a 2xx,
    False on anything else, and NEVER raises — a permanently-failing
    account must be invisible to the listener. Position is converted to
    MILLISECONDS here; our local bookmark is in seconds."""
    try:
        body = json.dumps({"consumableId": str(consumable_id),
                           "position": int(position_s * 1000),
                           "deviceId": device_id()}).encode()
        status, _h, _b = _api(BOOKMARK, method="POST", data=body,
                              headers={"Content-Type": "application/json"})
        return 200 <= status < 300
    except (OSError, RuntimeError, ValueError):
        return False


def outbox_flush():
    """Drain the queue: one POST per pending book, drop those that succeed,
    leave the rest for next time. Best-effort start to finish — swallows
    everything, returns the number still pending. Runs off the play path,
    so a dead account never touches a listening child."""
    box = _outbox_load()
    if not box:
        return 0
    remaining = {}
    for cid, rec in box.items():
        pos_s = (rec.get("pos_ms") or 0) / 1000.0
        if not push_bookmark(cid, pos_s):
            remaining[cid] = rec
    if len(remaining) != len(box):
        _outbox_save(remaining)
    return len(remaining)


# --- downloads: runs as a nice-19 subprocess off the sweeper ----------------
DISK_FLOOR = int(os.environ.get("VIBB_STORYTEL_FLOOR", str(1_500_000_000)))


def _free_bytes(path):
    try:
        import shutil
        return shutil.disk_usage(path).free
    except OSError:
        return 1 << 62      # unknown -> don't block on a phantom full disk


def _download(url, dest, timeout=120, resume=False):
    """Stream url -> dest via a .part temp then atomic rename. Inlined
    rather than borrowed from content.py so this module stays a
    self-contained bare script (see the import note at the top).

    resume continues an existing .part with a Range request; the caller
    re-mints the single-use signed url first, and a server that ignores
    the Range (answers 200) restarts cleanly instead of corrupting."""
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if resume and os.path.exists(tmp) else 0
    req = urllib.request.Request(
        url, headers={"Range": f"bytes={have}-"} if have else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        append = have and getattr(r, "status", 200) == 206
        with open(tmp, "ab" if append else "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    os.replace(tmp, dest)


def sync(target, count):
    """Download a target's books. `count` books in reading order, or all
    when count < 0. Writes shelf.json (the FULL listing, so the picker
    shows every book) but downloads only the requested prefix.

    Raises on an auth/network failure so the supervising _sync_one sees a
    non-zero exit; a killed mid-download leaves a .part that the next run
    resumes. Skips locked/geo books (they 403) and stops on a low disk
    rather than filling the card. One book at a time — the radio budget is
    the sweeper's, not ours to widen."""
    parsed = parse_target(target)
    if not parsed:
        return
    grp = next((g for g in normalize_shelf(bookshelf())
                if g["target"] == target), None)
    if not grp:
        _log(f"{target} is not on the shelf")
        return
    write_shelf(target, grp["name"], grp["books"])
    d = cache_dir(target)
    os.makedirs(d, exist_ok=True)
    cover_url = grp.get("cover")
    cover = os.path.join(d, "cover.jpg")
    if cover_url and not os.path.exists(cover):
        try:
            _download(cover_url, cover)
        except OSError as e:
            _log(f"cover {target}: {e}")
    books = grp["books"] if count < 0 else grp["books"][:max(count, 0)]
    for b in books:
        cid = b.get("consumable_id")
        if not cid or b.get("locked") or b.get("geo"):
            continue
        dest = book_path(target, cid)
        if os.path.exists(dest):
            continue                # already downloaded — incremental
        if _free_bytes(d) < DISK_FLOOR:
            _log(f"disk below floor, stopping before {cid}")
            return
        # Re-mint the signed url every attempt: it is single-use and
        # short-lived, and the CDN path is stable, so a resumed .part
        # sends its Range against a fresh token.
        url = asset_url(cid)
        _download(url, dest, timeout=600, resume=True)
        jpg = b.get("cover")
        if jpg:
            try:
                _download(jpg, os.path.join(d, f"{cid}.jpg"))
            except OSError:
                pass
        _log(f"downloaded {cid} ({b.get('title')})")


if __name__ == "__main__":
    # The sweep runs this as: python3 storytel.py sync <target> <count>
    if len(sys.argv) >= 4 and sys.argv[1] == "sync":
        try:
            sync(sys.argv[2], int(sys.argv[3]))
        except (OSError, RuntimeError, ValueError) as exc:
            _log(f"sync failed: {exc}")
            sys.exit(1)
    else:
        _log("usage: storytel.py sync <target> <count>")
        sys.exit(2)
