#!/usr/bin/env python3
"""Gate that pressing play NEVER blocks the UI thread.

/play is the slowest endpoint the screen calls: for a Spotify target
the daemon runs `systemctl is-active` (10s budget) plus two go_status()
round-trips (5s each) before it answers — a worst case far past the
screen's CONTROL_TIMEOUT. Called inline it froze the panel mid-press
and then gave up WITHOUT entering now-playing, so the box looked stuck
and idle while playback was actually starting (field 2026-08-02: a 6s
freeze on an album tile; the retry hit the daemon's resume shortcut and
felt instant).

Pinned here: the POST happens off-thread, now-playing is entered
optimistically, and the one answer the UI still needs ('no-internet')
is handed back to the MAIN thread — a background thread must never
draw."""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402


def app():
    a = ui.App.__new__(ui.App)
    a.view = "carousel"
    a.sel = 0
    a.stack = []
    a.dirty = False
    a.play_offline = False
    a.catch_up_until = 0.0
    a.last_status = 0.0
    a._poll_wake = threading.Event()
    a.draw_message = lambda *k, **kw: None
    return a


# 1. a SLOW daemon must not hold the caller: _play_async returns while
#    the POST is still in flight, and the screen is already on now-playing
POSTED = []
release = threading.Event()


def slow_post(path, body=None, timeout=None):
    POSTED.append((path, body))
    release.wait(10)          # the daemon is busy (systemctl + go_status)
    return {"source": "spotify"}


ui.api_post = slow_post
a = app()
t0 = time.monotonic()
a._play_async({"id": "e1"})
elapsed = time.monotonic() - t0
assert elapsed < 1.0, f"_play_async blocked the UI thread for {elapsed:.1f}s"
assert a.view == "now", f"must enter now-playing optimistically: {a.view}"
release.set()
for _ in range(50):           # let the poster finish
    if POSTED:
        break
    time.sleep(0.1)
assert POSTED == [("/play", {"id": "e1"})], POSTED
print("1. slow /play does not block the UI thread; now-playing entered OK")

# 2. a no-internet verdict is PARKED for the main thread, never drawn
#    from the poster (draw_message + sleep belong to the render thread)
ui.api_post = lambda path, body=None, timeout=None: {"error": "no-internet"}
a = app()
DREW = []
a._reconnect_for_spotify = lambda: DREW.append(1)
a._play_async({"id": "spot"})
for _ in range(50):
    if a.play_offline:
        break
    time.sleep(0.1)
assert a.play_offline, "the no-internet verdict must reach the main thread"
assert DREW == [], "the background thread must never run the drawing flow"
print("2. no-internet parked as a flag, not drawn off-thread OK")

# 3. a dead daemon (OSError) is survivable: no flag, no crash, and the
#    screen stays on now-playing (the poller corrects it if nothing plays)
def boom(path, body=None, timeout=None):
    raise OSError("connection refused")


ui.api_post = boom
a = app()
a._play_async({"id": "e1"})
time.sleep(0.5)
assert not a.play_offline, "a transport error is not a no-internet verdict"
assert a.view == "now"
print("3. dead daemon: no crash, no false offline verdict OK")

# 4. every /play call site goes through _play_async — an inline
#    api_post("/play"...) would reintroduce the freeze
src = open(os.path.join(REPO, "pi", "ui.py")).read()
helper = src[src.index("def _play_async"):]
helper = helper[:helper.index("\n    def ", 1)]
assert src.count('api_post("/play"') == 1, \
    "the only /play POST must be the one inside _play_async"
assert 'api_post("/play"' in helper, "…and it must live in the helper"
assert src.count("_play_async(") >= 4, \
    "expected the helper plus its three call sites"
print("4. no inline /play POSTs remain in ui.py OK")

print("\nall ui_play_async checks passed")
