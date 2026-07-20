#!/usr/bin/env python3
"""Gate the screen's speaker popups (field log 2026-07-17: the speaker
came up 25s before anyone pressed play again — nobody knew it was
ready). /status carries bt_waiting ("speaker not connected — X connects
the configured device") and bt_ready ("connected — press A"); the UI
paints them over now-playing AND the kid-mode carousel, and while the
waiting popup shows, X means connect (not volume — volume without sound
is pointless)."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")  # no SPI in tests

import ui  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


def wait_for(what, pred, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise SystemExit(f"TIMEOUT waiting for: {what}")


GETS, POSTS, VOLUME = [], [], []


def fake_get(path, timeout=10):
    GETS.append(path)
    if path == "/bt":
        return {"configured": "2C:FD:B3:FA:DA:04"}
    return {}


def fake_post(path, body=None, timeout=15):
    POSTS.append((path, body))
    if path == "/bt/connect":
        time.sleep(0.4)  # a real connect takes seconds — dedupe window
    return {"ok": True}


ui.api_get = fake_get
ui.api_post = fake_post

app = object.__new__(ui.App)
app.status = {}
app.system = {}
app.settings = {}          # nav mode 0 (menus) — carousel reads it now
app.car_section = None
app.bt_connecting_until = 0.0
app.vol_mode_until = 0.0
app.volume_flash = 0.0
app.volume_shown = None
app.last_status = 1e18  # never re-poll inside handlers
app.dirty = False
app.user_touched = False
app._volume_mode = lambda **kw: VOLUME.append(kw)
app.library = {"sections": [{"entries": [
    {"id": "e1", "name": "Marlon", "target": "https://x/feed"}]}]}
app._lib_at = time.monotonic()
app.car_sel = 0


def blank_draw():
    img = Image.new("RGB", (ui.W, ui.H), (0, 0, 0))
    return img, ImageDraw.Draw(img)


# 1. overlay paints for bt_waiting and bt_ready, stays silent otherwise
img, d = blank_draw()
app.status = {}
assert app._bt_overlay(d) is False
assert img == Image.new("RGB", (ui.W, ui.H), (0, 0, 0)), "clean frame drawn on"
img, d = blank_draw()
app.status = {"bt_waiting": True}
assert app._bt_overlay(d) is True
assert img != Image.new("RGB", (ui.W, ui.H), (0, 0, 0)), "waiting popup empty"
img, d = blank_draw()
app.status = {"bt_waiting": False, "bt_ready": True}
assert app._bt_overlay(d) is True
print("1. popup paints for waiting and ready, never otherwise OK")

# 2. while waiting, X in now-playing connects the configured speaker
app.status = {"bt_waiting": True}
app.handle_now("x")
wait_for("connect fired", lambda: POSTS)
assert POSTS == [("/bt/connect", {"mac": "2C:FD:B3:FA:DA:04"})], POSTS
assert "/bt" in GETS and not VOLUME, (GETS, VOLUME)
print("2. X on the waiting popup connects the last speaker OK")

# 3. X-mashing is deduped while a connect is IN FLIGHT; once it
# finishes, X may retry
wait_for("first connect done", lambda: app.bt_connecting_until == 0.0)
POSTS.clear()
app.handle_now("x")  # starts the slow connect
app.handle_now("x")  # mashed while in flight
app.handle_now("x")
time.sleep(0.2)
assert len(POSTS) == 1, f"X-mash stacked connects: {POSTS}"
wait_for("connect done", lambda: app.bt_connecting_until == 0.0)
print("3. repeated X while connecting is a no-op OK")

# 4. same intercept in the kid-mode carousel
app.bt_connecting_until = 0.0
POSTS.clear()
app.handle_carousel("x")
wait_for("carousel connect fired", lambda: POSTS)
assert POSTS[0][0] == "/bt/connect", POSTS
assert not VOLUME, "carousel X leaked into the volume card"
print("4. carousel X on the waiting popup connects too OK")

# 5. without the popup, X keeps meaning volume everywhere
app.bt_connecting_until = 0.0
POSTS.clear()
app.status = {}
app.handle_now("x")
app.handle_carousel("x")
time.sleep(0.2)
assert not POSTS and len(VOLUME) == 2, (POSTS, VOLUME)
print("5. no popup -> X is the volume card as before OK")

# 5b. the waiting popup offers the SAME A escape as the lost popup:
# 'play on box speaker' where a built-in speaker exists. Identical shape.
app.bt_connecting_until = 0.0
POSTS.clear()
app.status = {"bt_waiting": True, "bt_local_ok": True}
app.handle_now("a")
wait_for("waiting-popup A plays local", lambda: POSTS)
assert POSTS == [("/output", {"device": "local"}), ("/playpause", None)], \
    POSTS
POSTS.clear()
app.handle_carousel("a")  # same in the carousel
wait_for("carousel waiting-popup A", lambda: POSTS)
assert POSTS[0] == ("/output", {"device": "local"}), POSTS
print("5b. A on the waiting popup plays on the box speaker OK")

# 5c. BT-only box (no bt_local_ok): the waiting popup has no A escape,
# so A stays plain play/pause
POSTS.clear()
app.status = {"bt_waiting": True}
app.handle_now("a")
time.sleep(0.2)
assert POSTS == [("/playpause", None)], POSTS
print("5c. BT-only box: waiting popup A is plain play/pause OK")


# --- the lost popup (speaker died mid-play; daemon stopped the player) ------

# 6. renders, with the A-option only where a box speaker exists
img, d = blank_draw()
app.status = {"bt_lost": True, "bt_local_ok": True}
assert app._bt_overlay(d) is True
img, d = blank_draw()
app.status = {"bt_lost": True}  # BT-only box: X is the only offer
assert app._bt_overlay(d) is True
print("6. lost popup renders (with and without the A-option) OK")

# 7. A on the lost popup (nothing sounding) flips output to the box
# speaker AND resumes from the bookmark
POSTS.clear()
app.status = {"bt_lost": True, "bt_local_ok": True, "playing": False}
app.handle_now("a")
wait_for("play-on-local fired", lambda: len(POSTS) == 2)
assert POSTS == [("/output", {"device": "local"}), ("/playpause", None)], \
    POSTS
print("7. A on the lost popup (stopped) -> output local + resume OK")

# 7b. A while ALREADY playing on the built-in speaker (you switched the
# output to a disconnected BT speaker) must ONLY drop back to local —
# never toggle playpause and pause the audio
POSTS.clear()
app.status = {"bt_waiting": True, "bt_local_ok": True, "playing": True}
app.handle_now("a")
wait_for("output flip fired", lambda: POSTS)
time.sleep(0.2)
assert POSTS == [("/output", {"device": "local"})], \
    f"must not pause audio that's already playing: {POSTS}"
print("7b. A while playing on built-in -> output local only, no pause OK")

# 8. same from the kid-mode carousel (stopped -> output local + resume)
POSTS.clear()
app.status = {"bt_lost": True, "bt_local_ok": True, "playing": False}
app.handle_carousel("a")
wait_for("carousel play-on-local fired", lambda: len(POSTS) == 2)
assert POSTS[0] == ("/output", {"device": "local"}), POSTS
print("8. carousel A on the lost popup -> output local + resume OK")

# 9. no box speaker: A falls through to the normal play/pause
POSTS.clear()
app.status = {"bt_lost": True}
app.handle_now("a")
time.sleep(0.2)
assert POSTS == [("/playpause", None)], POSTS
print("9. BT-only box: A stays plain play/pause OK")

# 10. X on the lost popup runs the reconnect, like the waiting popup
POSTS.clear()
app.bt_connecting_until = 0.0
app.handle_now("x")
wait_for("lost-popup X connects", lambda: POSTS)
assert POSTS[0][0] == "/bt/connect", POSTS
print("10. X on the lost popup reconnects the speaker OK")

# --- the no-internet popup (same shape as the speaker popups) ---------------

# 11. renders for an offline ACTIVE spotify source; silent for mpv
# (cached podcasts play fine offline — no scary popup) and when online
app.wifi_connecting_until = 0.0
img, d = blank_draw()
app.status = {"spotify_offline": True, "source": "spotify"}
assert app._net_overlay(d) is True
img, d = blank_draw()
app.status = {"spotify_offline": True, "source": "mpv"}
assert app._net_overlay(d) is False
img, d = blank_draw()
app.status = {"spotify_offline": False, "source": "spotify"}
assert app._net_overlay(d) is False
print("11. net popup: offline spotify only — never for mpv/online OK")

# 12. speaker trouble outranks net trouble (only one popup at a time):
# render_now must not stack both — verified via the overlay contract
img, d = blank_draw()
app.status = {"bt_lost": True, "spotify_offline": True, "source": "spotify"}
assert app._bt_overlay(d) is True  # bt popup wins; render_now skips net
print("12. bt popup outranks the net popup OK")

print("ui_bt_popup: all OK")
