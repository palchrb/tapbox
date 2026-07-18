#!/usr/bin/env python3
"""Gate the now-view cover selection: a remote cover (Spotify album art,
gfx.nrk.no episode art) must be fetched OFF the render thread (never a
blocking sync fetch that stalls the UI), and the cached collection cover
(playlist mosaic / show cover) must fill in immediately so the card is
never blank offline or while the remote loads. Field bug 2026-07-17: a
reboot-resume to a bookmarked Spotify playlist showed no album art (the
remote fetch failed with no net yet and there was no local fallback)."""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")

import ui  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

ASYNC, SYNC = [], []


def make_app(status):
    app = object.__new__(ui.App)
    app.status = status
    app.system = {}
    app.wifi_connecting_until = 0.0
    app.bt_connecting_until = 0.0
    app.volume_flash = 0.0
    app.volume_shown = None
    app.vol_mode_until = 0.0
    thumb = Image.new("RGB", (128, 128), (10, 10, 10))
    app.artwork = lambda ref, size=110: (SYNC.append(ref), thumb)[1] if ref \
        else None
    app.artwork_async = lambda ref, size=110: (ASYNC.append(ref), None)[1]
    return app


def render(status):
    ASYNC.clear()
    SYNC.clear()
    img = Image.new("RGB", (ui.W, ui.H), (0, 0, 0))
    make_app(status).render_now(ImageDraw.Draw(img), img)


# 1. Spotify: remote album art goes to the async (off-thread) fetch, and
# the cached mosaic is drawn as the offline-proof fallback (sync, local)
render({"source": "spotify", "title": "Song", "spotify": {"artists": ["A"]},
        "artwork": "http://i.scdn.co/abc.jpg",
        "artwork_local": "/cache/mosaic.jpg"})
assert ASYNC == ["http://i.scdn.co/abc.jpg"], ASYNC
assert SYNC == ["/cache/mosaic.jpg"], SYNC
print("1. spotify remote art async + local mosaic fallback OK")

# 2. a per-item LOCAL cover (podcast episode art on disk) is used
# directly — no network fetch at all
render({"source": "mpv", "title": "Ep", "artwork": "/cache/show/e1.jpg",
        "artwork_local": "/cache/show/cover.jpg"})
assert SYNC[0] == "/cache/show/e1.jpg", SYNC
assert ASYNC == [], "a local cover must never hit the async fetcher"
print("2. local episode art used directly, no remote fetch OK")

# 3. remote episode art (online-only) also goes async, show cover fills in
render({"source": "mpv", "title": "Ep",
        "artwork": "https://gfx.nrk.no/x.jpg",
        "artwork_local": "/cache/show/cover.jpg"})
assert ASYNC == ["https://gfx.nrk.no/x.jpg"], ASYNC
assert SYNC == ["/cache/show/cover.jpg"], SYNC
print("3. remote episode art async + local show cover fallback OK")

# 4. nothing playing / no art -> no fetch, no crash
render({"source": None, "title": None})
assert ASYNC == [] and SYNC == [], (ASYNC, SYNC)
print("4. no artwork -> no fetch, clean render OK")

# 5. failure backoff escalates 5s -> 10 -> 20 ... capped at 60, and a
# success clears the ladder. Boot is fast enough that the resume's cover
# fetch races wifi and LOSES (URLError seconds before DHCP; field
# 2026-07-18) — a flat 60s backoff left the mosaic up a minute+ after
# the net was fine, so the first retry must come quickly.
import time as _t  # noqa: E402

app = object.__new__(ui.App)
app.artwork_cache = {}
app._art_pending = set()
app._art_fails = {}
app._art_key = lambda ref, size: (ref, size)
URL = "http://i.scdn.co/never-resolves.jpg"


def _no_net(*a, **k):  # hermetic: the fetch always fails, instantly
    raise OSError("no route")


ui.urllib.request.urlopen = _no_net

expected = [5, 10, 20, 40, 60, 60]
for i, want in enumerate(expected, 1):
    app.artwork_cache.pop((URL, 110), None)   # backoff elapsed -> retry
    r = app.artwork(URL)                       # fetch fails (no such host)
    assert r is None
    until = app.artwork_cache[(URL, 110)]
    delta = until - _t.monotonic()
    assert want - 2 < delta <= want + 1, (i, want, delta)
print("5. artwork failure backoff escalates 5,10,20,40,60 (capped) OK")

# 6. a later success resets the ladder (next failure starts at 5s again)
img = Image.new("RGB", (8, 8))
app.artwork_cache[(URL, 110)] = img            # simulate a fetch landing
app._art_fails.pop((URL, 110), None)           # what the success path does
app.artwork_cache.pop((URL, 110))
r = app.artwork(URL)
delta = app.artwork_cache[(URL, 110)] - _t.monotonic()
assert 3 < delta <= 6, delta
print("6. a success resets the backoff ladder to 5s OK")

print("UI COVER OK — remote off-thread, local fallback never blank, "
      "boot-race failures retry fast.")
