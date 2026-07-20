#!/usr/bin/env python3
"""Gate the bookmark SD-write throttle: the 5s Spotify bookmarker tick
used to json+rename onto the SD card every tick — 720 write bursts per
listening hour (energy audit 2026-07-20 #2). Now: write on track
change, every TAPBOX_BOOKMARK_FLUSH seconds otherwise, and the moment
the session stops yielding bookmarks (pause/stop/phone takeover) the
last throttled position flushes — pausing still bookmarks the pause
point, and only a hard power cut can lose <=30s of position."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = STATE
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402


class StopLoop(Exception):
    pass


SAVES = []
daemon._spotify.save_bookmark = lambda bm: SAVES.append(dict(bm))
daemon.ORCH.source = "spotify"
daemon.ORCH.target = "spotify:album:CTX"
daemon.ORCH._mpv_alive = lambda: False


def st(uri, pos=1000, paused=False):
    return {"track": {"uri": uri, "position": pos, "duration": 100000,
                      "name": "t", "artist_names": [],
                      "album_cover_url": None},
            "paused": paused, "stopped": False,
            "play_origin": "go-librespot"}


def run_bm(states, flush="30"):
    os.environ["TAPBOX_BOOKMARK_FLUSH"] = flush
    SAVES.clear()
    ticks = list(states)

    def status():  # each tick consumes one scripted /status answer
        return ticks.pop(0)

    daemon.go_status = status
    real_wake = daemon._bm_wake

    class FakeWake:  # never blocks; ends the loop when the script is done
        def wait(self, _s):
            if not ticks:
                raise StopLoop
            return False

        def clear(self):
            pass

    daemon._bm_wake = FakeWake()
    try:
        daemon._spotify_bookmarker()
    except StopLoop:
        pass
    finally:
        daemon._bm_wake = real_wake
    return SAVES


# 1. same track, three quick ticks: exactly ONE write (was three)
out = run_bm([st("spotify:track:T1", 1000),
              st("spotify:track:T1", 6000),
              st("spotify:track:T1", 11000)])
assert len(out) == 1 and out[0]["position"] == 1000, out
print("1. steady playback writes once per flush window, not per tick OK")

# 2. a track change writes immediately (resume must survive skips)
out = run_bm([st("spotify:track:T1"), st("spotify:track:T2")])
assert len(out) == 2 and out[1]["uri"] == "spotify:track:T2", out
print("2. track change flushes immediately OK")

# 3. pausing flushes the last throttled position — the pause point is
# the bookmark, not a spot up to 30s earlier
out = run_bm([st("spotify:track:T1", 1000),
              st("spotify:track:T1", 6000),
              st("spotify:track:T1", 6500, paused=True)])
assert len(out) == 2 and out[1]["position"] == 6000, out
print("3. pause flushes the freshest throttled position OK")

# 4. flush window elapsed: writes again (env-tunable)
out = run_bm([st("spotify:track:T1", 1000),
              st("spotify:track:T1", 6000)], flush="0")
assert len(out) == 2, out
print("4. positions keep landing every flush window OK")

print("BM THROTTLE OK — SD writes per listening hour drop ~6x, pause "
      "and skip points still land exactly, worst power-cut loss 30s.")
