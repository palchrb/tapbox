#!/usr/bin/env python3
"""The resume overlap: after an outage, back up a beat.

When the output dies mid-story the box resumes at the bookmark — but
the bookmark is where the DECODER was, not where the child's ears
were, and audio sits buffered in mpv and again in the speaker. So a
plain resume drops a sentence. Backing up ~3s makes the child hear one
clause twice instead, which nobody registers as an event.

Two things this pins that are easy to get wrong:
- the overlap is applied where start_pos is COMPUTED, not at the seek.
  The daemon holds the reported position at the published resume_pos
  until playback reaches it, so subtracting at the seek would freeze
  the progress bar for the whole overlap (QA 2026-08-13).
- spotify never reaches the mpv branch, so it needs its own subtraction
  in play_spotify — and it needs exact=True with it, or a rewind can
  push the position under SPOT_RESUME_MIN_MS and the track restarts."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402
import daemon  # noqa: E402

PSRC = open(player.__file__, encoding="utf-8").read()
DSRC = open(daemon.__file__, encoding="utf-8").read()

# 1. the overlap lands on start_pos BEFORE the now-playing publish —
#    otherwise /status holds the display at the un-rewound position
i_apply = PSRC.index("start_pos = max(0.0, start_pos - rewind)")
i_publish = PSRC.index("# Publish the FIRST item before mpv even starts")
i_seek = PSRC.index('ipc(sock, "seek"')
assert i_apply < i_publish, \
    "the overlap must be applied before the resume_pos publish"
assert i_apply < i_seek, "and before any seek"
assert "start_pos - rewind" in PSRC and "max(0.0," in PSRC
print("1. overlap applied at start_pos, ahead of the publish OK")

# 2. argv: --rewind parses, clamps at 0, and survives a junk value
assert '"--rewind"' in PSRC
assert "rewind = max(0.0, float(args[1]))" in PSRC
assert "except (IndexError, ValueError):" in PSRC
print("2. --rewind parsed, clamped, junk-tolerant OK")

# 3. spotify subtracts in its OWN branch (milliseconds, clamped), since
#    the mpv branch never runs for those targets
assert "def play_spotify(target, fresh=False, exact=False, start_uri=None," \
    in PSRC
assert "rewind=0.0):" in PSRC
assert "pos_ms = max(0, int(bm[\"position\"]) - int(rewind * 1000))" in PSRC
assert "start_uri=episode, rewind=rewind)" in PSRC
print("3. spotify subtracts from the bookmark in ms, clamped OK")

# 4. the policy lives in the DAEMON: a player process sees one fault and
#    cannot know how long the output was gone
orch = object.__new__(daemon.Orchestrator)
orch.source = "mpv"
orch._crash_respawns = 0
daemon._BT_WAIT["lost"] = 0.0
assert orch._resume_overlap() == daemon.RESUME_OVERLAP_SPEECH_S
assert orch._resume_overlap("spotify") == daemon.RESUME_OVERLAP_MUSIC_S
# a long outage earns a run-up: the child stopped listening
daemon._BT_WAIT["lost"] = daemon.time.monotonic() - 600
assert orch._resume_overlap() == daemon.RESUME_OVERLAP_LONG_S
# ...but a flapping speaker must not walk the story backwards
orch._crash_respawns = 3
daemon._BT_WAIT["lost"] = daemon.time.monotonic() - 5
assert orch._resume_overlap() == 0.0, \
    "repeated faults in seconds must not each rewind"
daemon._BT_WAIT["lost"] = 0.0
print("4. policy: clause / beat / run-up / flap-guard OK")

# 5. _spawn plumbs it, and only when non-zero (so nothing changes for a
#    normal tap)
assert "cache=None, resume=True, exact=False, rewind=0.0):" in DSRC
assert 'if rewind:\n            args += ["--rewind", f"{rewind:g}"]' in DSRC
print("5. _spawn passes --rewind only when there is one OK")

# 6. exact=True reaches the three paths that resume a SPOTIFY session
#    after a fault. The other _spawn sites are mpv-only (where --exact is
#    dead code) or user taps, and must stay untouched.
def spawn_window(marker, span=420):
    i = DSRC.index(marker)
    return DSRC[i:i + span]


# rule 4: the child presses play/next/prev on a session a fault killed
w = spawn_window('resume=self.resume or session_resume()')
assert "exact=True" in w, "the press-to-resume path needs exact"
# bt connect completed after an interruption
w = spawn_window("def _bt_resume(resume):")
assert "exact=True" in w
# speaker back inside the blip window, spotify branch. The marker is
# the full log line on purpose: 'resuming spotify' also matches the
# output-switch resume, which already carried exact=True.
w = spawn_window("speaker back within the blip window — resuming spotify")
assert "exact=True" in w
print("6. exact=True on the three spotify fault-resume paths OK")

# 7. there is no boot-resume call left to carry a rewind: boot lands
#    PAUSED (2026-08), so the overlap only ever applies to a fault
#    resume or a real press — never to a power-on.
assert "def _boot_resume" not in DSRC, \
    "boot resume is gone — the box lands paused and one tap continues"
print("7. no boot starter, so no boot rewind to get wrong OK")

print("\nRESUME OVERLAP OK — a clause repeated, never a sentence lost.")
