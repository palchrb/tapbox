#!/usr/bin/env python3
"""Gate the play_spotify retry against go-librespot's BLOCKING api. An
aborted /player/play usually KEEPS executing server-side (field
2026-07-18 20:26: a 'dropped' /next landed 15s later) — so on a client
timeout the player must CHECK whether the request landed before
re-POSTing, or it reloads the whole context behind the first request
(double CDN fetch, RF review 2026-07-18). And when the server genuinely
failed (audio key 'context deadline exceeded', field 20:04), the fast
8s abort + 1s pause beats the old 15s + 3s by ~9s per attempt."""
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402

CTX = "spotify:album:A"
BM_URI = "spotify:track:T"
CURRENT = {"track": None}
GO, SCRIPT = [], []


def status(timeout=5):
    return {"username": "u", "track": CURRENT["track"]}


PLAY_BODY = {}


def go(path, timeout=15, body=None):
    GO.append(path)
    if path != "/player/play":
        return  # (no seek/resume any more — position rides the play call)
    PLAY_BODY.clear()
    PLAY_BODY.update(body or {})
    act = SCRIPT.pop(0)
    if act == "ok":
        CURRENT["track"] = {"uri": BM_URI}
        return
    if act == "timeout-lands":
        # the CLIENT gave up but the server finished the load anyway
        CURRENT["track"] = {"uri": BM_URI}
    raise OSError("timed out")


player.radio = types.SimpleNamespace(touch_busy=lambda: None,
                                     wait_paging_clear=lambda cap_s=6: None)
player.spotify = types.SimpleNamespace(
    to_uri=lambda t: CTX,
    clear_bookmark=lambda uri: None,
    read_bookmark=lambda uri: {"context_uri": CTX, "uri": BM_URI,
                               "position": 30000},
    status=status, go=go)
player._apply_box_volume = lambda: None


def run(script, pre_track):
    GO.clear()
    SCRIPT[:] = script
    CURRENT["track"] = pre_track
    player.play_spotify("https://open.spotify.com/album/A")
    return [p for p in GO if p == "/player/play"]


# 1. timeout but the request LANDED (track loaded) -> NO re-POST; the
# bookmark position rides the play body (fork v0.0.5, no separate
# seek/resume), so resume still lands exactly where we left off
plays = run(["timeout-lands"], pre_track=None)
assert len(plays) == 1, f"landed request must not be re-POSTed: {GO}"
assert PLAY_BODY.get("position") == 30000, PLAY_BODY
assert "/player/seek" not in GO and "/player/resume" not in GO, GO
print("1. timed-out play that landed: no duplicate load, position in body OK")

# 2. timeout and the server genuinely failed (track unchanged) -> ONE
# retry, which succeeds against the warm server (position still carried)
plays = run(["timeout-dead", "ok"], pre_track={"uri": "spotify:track:OLD"})
assert len(plays) == 2, f"a dead attempt must be retried: {GO}"
assert PLAY_BODY.get("position") == 30000, PLAY_BODY
print("2. genuinely failed play: one fast retry, position rides the play OK")

# 3. never lands, all attempts dead -> exits nonzero with the hint
try:
    run(["timeout-dead"] * 3, pre_track=None)
    raise AssertionError("must exit when the api never answers")
except SystemExit as e:
    assert e.code == 1, e.code
print("3. api never answers: clean exit 1 (supervisor's problem now) OK")

# 4. the timeout constant is env-tunable and defaults to the fast 8s
assert player.PLAY_TIMEOUT_S == 8.0
print("4. first-attempt timeout defaults to 8s OK")

print("SPOT PLAY RETRY OK — landed requests are never doubled, dead "
      "ones retry fast against a warm server.")
