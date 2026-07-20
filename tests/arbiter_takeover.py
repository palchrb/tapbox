#!/usr/bin/env python3
"""Gate the phone-takeover arbiter. It yields mpv to Spotify ONLY when a
podcast (source=mpv) is what's playing AND a genuine phone session
appears (play_origin is not the box). It must NEVER fire on the box's
own Spotify: self.child is player.py for Spotify targets too, so
'child alive + spotify playing' is not proof of a phone — the boot-
resume into a Spotify playlist logged a phantom 'spotify took over
(phone)' and killed its own player (field 2026-07-20 08:18:39)."""
import os
import sys
import tempfile
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH


class StopLoop(Exception):
    pass


def run_arbiter(source, go_st, alive=True, age=99):
    """One-ish pass of the arbiter with everything stubbed. Returns
    True if it yielded (stopped the child + flipped source to spotify)."""
    orch.source = source
    orch.child_started = _time.monotonic() - age
    orch._mpv_alive = lambda: alive
    stopped = [False]

    def fake_stop():
        stopped[0] = True
        orch._mpv_alive = lambda: False  # child is gone after a stop

    orch._stop_child = fake_stop
    orch._persist = lambda: None
    daemon.go_status = (go_st if callable(go_st) else (lambda **k: go_st))

    left = [1]

    def fake_tick(_s):
        if left[0] <= 0:
            raise StopLoop
        left[0] -= 1

    real_tick = daemon._tick
    daemon._tick = fake_tick
    try:
        orch._arbiter()
    except StopLoop:
        pass
    finally:
        daemon._tick = real_tick
    return stopped[0]


PHONE = {"track": {"uri": "spotify:track:x"}, "paused": False,
         "play_origin": "spotify_ios"}
BOX = {"track": {"uri": "spotify:track:x"}, "paused": False,
       "play_origin": "go-librespot"}
IDLE = {"track": None, "paused": False, "play_origin": None}

# 1. THE BUG: box plays its own Spotify (source=spotify, child alive,
# spotify playing) -> the arbiter must NOT yield / must not log a phantom
# takeover, whatever the origin looks like
assert run_arbiter("spotify", BOX) is False
assert run_arbiter("spotify", PHONE) is False  # even a phone-ish origin
print("1. box's own Spotify never triggers a phantom takeover OK")

# 2. genuine takeover: a podcast is playing (source=mpv) and a PHONE
# Spotify session appears -> yield mpv
assert run_arbiter("mpv", PHONE) is True
print("2. phone Spotify over a live podcast yields mpv OK")

# 3. a box-origin Spotify blip while a podcast plays is NOT the phone ->
# don't yield (a switch race must not kill the podcast)
assert run_arbiter("mpv", BOX) is False
print("3. box-origin Spotify over a podcast does not yield OK")

# 4. nothing playing on Spotify -> nothing to yield to
assert run_arbiter("mpv", IDLE) is False
print("4. idle Spotify never yields OK")

# 5. grace window: a just-started podcast (age<10s) is ignored even if a
# phone session is already visible (player.py is still settling)
assert run_arbiter("mpv", PHONE, age=3) is False
print("5. young podcast inside the grace window is left alone OK")

# 6. no child alive -> nothing to yield
assert run_arbiter("mpv", PHONE, alive=False) is False
print("6. no live child means no takeover OK")

print("ARBITER TAKEOVER OK — only a real phone session over a live "
      "podcast yields; the box's own Spotify is never mistaken for it.")
