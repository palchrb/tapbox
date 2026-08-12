#!/usr/bin/env python3
"""The marquee phase anchor. Field 2026-08-12: marquee() phased on the
GLOBAL wall clock, so landing on a tile with a long title showed a
random middle of the name — as if the text were centered — instead of
its start. The anchor (App._marquee_t0) pins step 0 to the moment the
label became selected: rest at the START, then slide."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
os.environ["TAPBOX_EMOJI"] = "0"

import ui  # noqa: E402

LONG = "Fantorangen og den utrolig lange episodetittelen"
real_mono = time.monotonic
try:
    now = [1000.0]
    ui.time.monotonic = lambda: now[0]

    # 1. a FRESH anchor shows the START of the title, whatever the
    #    wall clock says (1000.0 is deep mid-phase for the old code)
    win, rolls = ui.marquee(LONG, 20, t0=now[0])
    assert rolls is True
    assert win == LONG[:20], f"fresh selection must show the start: {win!r}"
    print("1. landing shows the start of the title OK")

    # 2. it RESTS there for the lead-in steps, then slides
    now[0] += ui.MARQUEE_STEP_S * 3.5   # still inside the 4 rest steps
    win, _ = ui.marquee(LONG, 20, t0=1000.0)
    assert win == LONG[:20], "must still be resting at the start"
    now[0] += ui.MARQUEE_STEP_S * 4     # now past the rest steps
    win, _ = ui.marquee(LONG, 20, t0=1000.0)
    assert win != LONG[:20], "must have started sliding"
    print("2. rests at the start, then slides OK")

    # 3. the App anchor resets when the selected label changes,
    #    and holds while it does not
    app = object.__new__(ui.App)
    t0a = app._marquee_t0("tile", "Album A")
    now[0] += 5.0
    assert app._marquee_t0("tile", "Album A") == t0a, "same label holds"
    t0b = app._marquee_t0("tile", "Album B")
    assert t0b == now[0], "new label re-anchors to now"
    t0c = app._marquee_t0("episodes", 3)
    assert t0c == now[0], "view change re-anchors too"
    print("3. anchor holds per label, resets on change OK")

    # 4. default t0=0.0 is the old behaviour — existing callers/tests
    #    (frozen-clock ui_marquee.py) are untouched by the signature
    win_old, _ = ui.marquee(LONG, 20)
    win_new, _ = ui.marquee(LONG, 20, t0=0.0)
    assert win_old == win_new
    print("4. default keeps legacy phase — old pins unaffected OK")
finally:
    ui.time.monotonic = real_mono

print("\nMARQUEE ANCHOR OK — the start of the name is what you land on.")
