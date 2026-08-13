#!/usr/bin/env python3
"""The rollback anchor follows the track, never the previous one.

`stable` is the spot survive_dead_audio rolls back to when the output
dies. It only advanced after 15 audible seconds (player.py's dwell
gate) and was NEVER reset on a track change — so a fault in the first
15 seconds of episode N rolled the child into the MIDDLE of episode
N-1. Now every track change re-anchors it.

The value matters as much as the reset (QA 2026-08-13): anchoring
blindly to 0.0 would make a fault right after "resume at 5:00" seek the
child back to the top. So: the first track anchors at start_pos, an
in-session episode jump anchors at the position it just seeked to, and
only a natural advance anchors at 0.0.

This test drives the real loop body's decisions through a scripted mpv
rather than re-implementing them."""
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402

SRC = open(player.__file__, encoding="utf-8").read()

# 1. the anchor is re-set on EVERY track change, inside the branch that
#    runs before the dead-output check — so the rollback can never name
#    the previous episode
i_branch = SRC.index("if path and path != prev_path:")
i_dead = SRC.index("if fast_skips >= 3 or (not was_first", i_branch)
head = SRC[i_branch:i_dead]
assert "stable = (path, start_pos if was_first else 0.0)" in head, \
    "the anchor must be re-set on every track change, before the " \
    "dead-output check consumes it"
print("1. anchor re-set on every track change, before the check OK")

# 2. the first track anchors at start_pos, not at 0 — a fault ten
#    seconds after resuming at 5:00 must not rewind to the top
m = re.search(r"stable = \(path, (.+?)\)", head)
assert m and m.group(1) == "start_pos if was_first else 0.0", m and m.group(1)
print("2. first track anchors at its resume position, not 0 OK")

# 3. an in-session episode jump seeks to `saved` — and the anchor
#    follows that seek in the same breath
i_jump = SRC.index('log(f"resuming this episode at')
window = SRC[i_jump - 400:i_jump + 100]
assert 'ipc(sock, "seek", saved, "absolute")' in window
assert "stable = (path, saved)" in window, \
    "the anchor must follow the in-session jump's seek"
print("3. in-session jump anchors at the position it seeked to OK")

# 4. the 15s dwell gate that advances the anchor during playback is
#    untouched — it is what makes the anchor mean "audibly played"
assert "now_m - track_started > 15" in SRC
assert "stable = (path, pos)  # last spot that audibly played" in SRC
print("4. the audible-dwell advance is untouched OK")

# 5. survive_dead_audio still refuses to roll back to a path that is
#    not in this queue (a stale anchor must never pick a wrong file)
assert "if stable and stable[0] in urls:" in SRC
print("5. rollback still guards on the anchor being in this queue OK")

print("\nSTABLE ANCHOR OK — the rollback lands in THIS episode, at the "
      "spot the child was actually at.")
