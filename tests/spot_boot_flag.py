#!/usr/bin/env python3
"""Gate the box-initiated-Spotify boot-resume flag.

Symptom: mpv content reliably resumes on power-on, Spotify almost never
did. Root cause: _flag_was_playing (SIGTERM) decided Spotify's 'was
playing' with a LIVE go_status() query — but poweroff TERMs go-librespot
in the same cgroup, so that query races its death and reads 'not playing'.
mpv sidesteps the identical race via its now-playing.json fallback; Spotify
had none.

Fix: the bookmarker (already polling while OUR spotify plays) stamps
_SPOT_LAST_PLAYING from a status fetched while go-librespot is still alive,
and _flag_was_playing trusts that instead of racing the shutdown query.
Box-initiated ONLY (source==spotify AND a spotify target) so a phone-driven
Connect session never arms boot-resume."""
import json
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
orch.child = None  # no mpv session: isolate the spotify branch


def seed_last(**over):
    d = {"target": "https://open.spotify.com/playlist/xyz",
         "source": "spotify", "was_playing": False}
    d.update(over)
    with open(daemon.LAST_FILE, "w") as f:
        json.dump(d, f)


def was_playing():
    with open(daemon.LAST_FILE) as f:
        return json.load(f)["was_playing"]


# --- the shutdown contract (_flag_was_playing) ------------------------------

# 1. THE FIX: go-librespot already dead at shutdown (live query says 'no'),
#    but the bookmarker saw us playing a moment ago -> we still resume.
daemon.spotify_playing = lambda *a, **k: False  # go-librespot gone
daemon._SPOT_LAST_PLAYING[0] = True
seed_last()
daemon._flag_was_playing()
assert was_playing() is True, "cached playing state must survive the TERM race"
print("1. shutdown trusts the cached flag when the live query races death OK")

# 2. NO FALSE POSITIVE: paused/off box (cache false, live false) stays quiet
daemon._SPOT_LAST_PLAYING[0] = False
seed_last()
daemon._flag_was_playing()
assert was_playing() is False, "a box that was not playing must not resume"
print("2. a paused/off box is not flagged as playing OK")

# 3. LIVE FALLBACK PRESERVED: cache false but a live query CAN still answer
#    (e.g. go-librespot survived) -> that path keeps working
daemon._SPOT_LAST_PLAYING[0] = False
daemon.spotify_playing = lambda *a, **k: True
seed_last()
daemon._flag_was_playing()
assert was_playing() is True, "live probe must remain a valid fallback"
print("3. live-probe fallback still captures playback OK")


# --- the bookmarker wiring (sets _SPOT_LAST_PLAYING) ------------------------

# real predicate, driven by a stubbed status; neutralise bookmark writes so
# only the flag assignment is under test
daemon.spotify_playing = daemon._spotify.playing
daemon._spotify.bookmark_step = lambda *a, **k: None
_PLAYING = {"track": {"uri": "t", "position": 1000, "duration": 200000},
            "paused": False, "stopped": False, "play_origin": "go-librespot"}
daemon.go_status = lambda **k: _PLAYING

threading.Thread(target=daemon._spotify_bookmarker, daemon=True).start()


def one_tick():
    daemon._bm_wake.set()
    time.sleep(0.3)  # one iteration, then it blocks on the next wait


# 4. box-initiated spotify playing -> flag goes true
daemon._SPOT_LAST_PLAYING[0] = False
orch.source = "spotify"
orch.target = "https://open.spotify.com/playlist/xyz"
one_tick()
assert daemon._SPOT_LAST_PLAYING[0] is True, \
    "bookmarker must arm the flag for box-initiated spotify"
print("4. bookmarker arms the flag for box-initiated spotify OK")

# 5. phone-driven Connect (box owns no spotify target) -> flag stays false
orch.source = "spotify"
orch.target = None  # nothing the box can replay
one_tick()
assert daemon._SPOT_LAST_PLAYING[0] is False, \
    "a phone-driven session must not arm boot-resume"
print("5. phone-driven / target-less session does not arm the flag OK")

print("\nall spot_boot_flag checks passed")
