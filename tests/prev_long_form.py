#!/usr/bin/env python3
"""Prev stops meaning "restart" once the file is long enough to matter.

Prev has always meant: past 5 seconds, restart this track; within 5
seconds of the start, go to the previous one. Standard player
semantics, and harmless on a 30-minute podcast episode.

On an audiobook it is not harmless. The restart issues `seek 0`, the
player's poll reads position ~0 and persists it within 33 seconds, and
NOTHING in save_state protects a large existing value. So one prev at
hour six of an eleven-hour book costs six hours, permanently.

Worse, that is not the accident case — it is the NORMAL case. Browsing
backwards through a series costs two presses per book (restart, then
previous), so walking back three books destroys three bookmarks on the
way. The owner spotted this before it was built.

The rule exists because there was no seek. Since 2026-08-14 there is
one, and holding B on the seek card reaches the start of a short
episode in about two seconds — so restart is no longer prev's unique
job, and it can be given up exactly where it does damage.

Gated on DURATION (owner: 30 minutes, 2026-08-14), not on where the
file came from: a three-hour podcast has precisely the same problem as
an audiobook, and a Storytel-only rule would miss it."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()

import daemon  # noqa: E402

SENT = []


def arm(pos, dur, ppos=3, count=8):
    """A live mpv session at `pos` seconds into a file of `dur`."""
    SENT.clear()
    props = {"playback-time": pos, "duration": dur,
             "playlist-pos": ppos, "playlist-count": count}
    daemon.mpv_get = lambda p, **k: props.get(p)
    daemon.mpv_ipc = lambda cmd, **k: (SENT.append(cmd)
                                       or {"error": "success"})
    o = object.__new__(daemon.Orchestrator)
    o.lock = daemon.threading.RLock()
    o.source = "mpv"
    o._mpv_alive = lambda: True
    o.target = "t"
    o.resume = None
    return o


REAL = (daemon.mpv_get, daemon.mpv_ipc, daemon._kick_bt_connect,
        daemon._radio.touch_user_skip)
daemon._kick_bt_connect = lambda *a, **k: None
daemon._radio.touch_user_skip = lambda: None

try:
    # 1. a short episode is unchanged: 20 minutes in, prev restarts it
    o = arm(pos=1200.0, dur=1800.0 - 1)
    o._command_locked("prev")
    assert SENT == [["seek", 0, "absolute"]], SENT
    print("1. under the limit, prev still restarts the track OK")

    # 2. THE ONE THAT MATTERS: six hours into an eleven-hour book, prev
    #    steps back a book. It must NOT seek — a seek to 0 here is the
    #    six hours, gone within 33 seconds and unrecoverable.
    o = arm(pos=21600.0, dur=39600.0)
    o._command_locked("prev")
    assert SENT == [["playlist-prev"]], SENT
    assert not any(c and c[0] == "seek" for c in SENT), \
        "a long file must never be restarted by prev"
    print("2. over the limit, prev steps back and never seeks OK")

    # 3. and it holds at every depth, including the depth where the old
    #    rule was most tempting — browsing backwards must cost ONE press
    #    per book, not two, or every book passed loses its place
    for pos in (6.0, 60.0, 3600.0, 39000.0):
        o = arm(pos=pos, dur=39600.0)
        o._command_locked("prev")
        assert SENT == [["playlist-prev"]], (pos, SENT)
    print("3. browsing back through a long series never restarts OK")

    # 4. the wrap is untouched: at slot 0 prev still jumps to the END of
    #    the queue (field 2026-07-18 — resume rotates the queue, so
    #    playlist-prev is a no-op there and prev did nothing at all)
    o = arm(pos=21600.0, dur=39600.0, ppos=0, count=8)
    o._command_locked("prev")
    assert SENT == [["set_property", "playlist-pos", 7]], SENT
    print("4. the slot-0 wrap still works on long files OK")

    # 5. a LIVE stream has no duration. It must fall through to the old
    #    behaviour rather than being treated as long-form — `None > n`
    #    would raise, and a bare truthiness test would misread it.
    o = arm(pos=600.0, dur=None)
    o._command_locked("prev")
    assert SENT == [["seek", 0, "absolute"]], SENT
    print("5. an unknown duration behaves as before, and never raises OK")

    # 6. the boundary is the owner's number and is overridable, so a
    #    field surprise is an env var rather than a redeploy
    assert daemon.PREV_RESTART_MAX_S == 1800.0, daemon.PREV_RESTART_MAX_S
    assert "VIBB_PREV_RESTART_MAX" in open(daemon.__file__).read()
    print("6. the limit is 30 minutes, and tunable without a code change OK")
finally:
    (daemon.mpv_get, daemon.mpv_ipc, daemon._kick_bt_connect,
     daemon._radio.touch_user_skip) = REAL

print("\nPREV LONG-FORM OK — prev restarts a podcast and steps back a "
      "book, and an eleven-hour place survives being browsed past.")
