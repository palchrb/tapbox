#!/usr/bin/env python3
"""The session stamp and the 'always from the start' contract.

Two halves of the same design (2026-08-13):
1. At shutdown the daemon records WHEN it stopped — but only with a
   clock it trusts. A stamp taken on the pre-RTC clock would read up to
   an hour stale next boot, and an hours-scale window can't survive
   that. No stamp = the window can't judge = continue (the pre-window
   behaviour), so a mid-flight upgrade never loses its resume.
2. A resume:false entry now PERSISTS its position (it wrote nothing
   before) so the session can continue it — while a TAP still starts it
   at track 1, because player.py enforces that flag when READING."""
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import paths  # noqa: E402
import daemon  # noqa: E402

TARGET = "https://open.spotify.com/album/xyz"


def write_last(**kw):
    d = {"target": TARGET, "source": "mpv", "reverse": False, "resume": False}
    d.update(kw)
    with open(daemon.LAST_FILE, "w") as f:
        json.dump(d, f)


def read_last():
    with open(daemon.LAST_FILE) as f:
        return json.load(f)


# the flag-writer's view of "was anything playing": say yes
daemon.ORCH.child = None
daemon.ORCH.source = "mpv"
daemon._SPOT_LAST_PLAYING[0] = True

# 1. with a trusted clock the shutdown stamps both flag and time
paths.note_clock_ok()
write_last()
before = time.time()
daemon._flag_was_playing()
last = read_last()
assert last["was_playing"] is True
assert before <= last["stopped_at"] <= time.time()
print("1. clean shutdown stamps was_playing + stopped_at OK")

# 2. an untrusted clock stamps NO time — better no signal than a wrong
#    one (the stamp would be up to an hour stale next boot)
os.remove(paths.CLOCK_OK_FILE)
write_last()
daemon._flag_was_playing()
last = read_last()
assert last["was_playing"] is True
assert not last["stopped_at"], "an untrusted clock must not stamp a time"
print("2. untrusted clock: flag yes, time no OK")

# 3. an unstamped slot resolves to 'continue' (upgrade path)
assert daemon.session_age_verdict(
    last["stopped_at"], 6, time.time(), True) == "fresh"
print("3. an unstamped session still continues — no upgrade regression OK")

# 4. ORCH captures the stamp at construction: _persist() drops it as
#    soon as anything plays, so a later read would see nothing
paths.note_clock_ok()
write_last(stopped_at=1_760_000_000.0)
orch = daemon.Orchestrator()
assert orch.boot_stopped_at == 1_760_000_000.0
orch.target, orch.source = TARGET, "mpv"
orch._persist()
assert not read_last().get("stopped_at"), \
    "a new session must not inherit the old stop time"
assert orch.boot_stopped_at == 1_760_000_000.0, "the snapshot survives"
print("4. stamp captured at construction, dropped when playback starts OK")

# 5. the resume:false contract, both directions. player.py's own gates:
#    a --no-resume spawn CLEARS the file and ignores it (tap = track 1),
#    while the position is written for every entry (session continuity).
import player  # noqa: E402
src = open(player.__file__).read()
assert "if fresh or no_resume:\n        clear_state(key)" in src, \
    "a tap on an 'always from start' entry must still wipe the bookmark"
assert "st = None if no_resume else load_state(key)" in src, \
    "a tap on an 'always from start' entry must still ignore the bookmark"
assert "if not live and not no_resume:" not in src, \
    "the WRITE gate must be gone — the session needs the position"
assert "                if not live:" in src
print("5. resume:false: writes a position, still taps to track 1 OK")

# 6. boot resume asks for the session, not the entry's flag — otherwise
#    player.py would clear the very bookmark it is meant to continue
import re  # noqa: E402
d_src = open(daemon.__file__).read()
call = re.search(r"ORCH\.play\(target, reverse=bool\(last\.get\(\"reverse\"\)\),"
                 r"\s*\n\s*resume=(\w+), boot=True\)", d_src)
assert call and call.group(1) == "True", \
    f"boot resume must spawn with resume=True, got {call and call.group(1)}"
print("6. boot resume spawns session-driven (resume=True) OK")

print("\nSESSION STAMP OK — an honest clock or none at all, and a music "
      "entry that continues without forgetting how to start over.")
