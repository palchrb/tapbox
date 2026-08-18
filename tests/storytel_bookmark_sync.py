#!/usr/bin/env python3
"""The bookmark mirror: one-way out, offline-safe, and silent when off.

The local bookmark file is the source of truth and is untouched — this
only READS it and queues changed positions to Storytel, which drain when
online and hold when not. The properties that matter:

  - the toggle off means ZERO network, ever;
  - a changed position is pushed once; an UNCHANGED one is not re-pushed
    (in-process dedup), so a book playing steadily does not POST every
    tick — the 660-requests-per-book trap;
  - offline, the position queues and nothing is lost or raised;
  - positions cross in milliseconds, carrying the stable device id."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
os.environ["VIBB_STORYTEL_CREDS"] = os.path.join(TMP, "creds.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import storytel, library, bookmarks  # noqa: E402

TARGET = "storytel:series:26175"
library.save_library({"version": 1, "sections": [{"id": "s", "name": "Lyd",
    "entries": [{"id": "e", "name": "Kokos", "target": TARGET,
                 "order": "oldest_first", "cache": -1, "resume": True}]}]})

# a local bookmark for two books in that series (what player.py writes)
key = library.state_key(TARGET)
bookmarks.save_state(key, "/c/111.mp3", 640.0, "111", 700.0)
bookmarks.save_state(key, "/c/222.mp3", 120.0, "222", 700.0)

storytel.save_credentials("k@vibb.me", "pw")   # an account to push under
PUSHED = []


def push_fake(url, method="GET", headers=None, data=None, timeout=15,
              follow=True):
    if "login.action" in url:
        return 200, {}, json.dumps({"accountInfo": {"jwt": "J"}}).encode()
    if url.endswith("/bookmarks/positional"):
        PUSHED.append(json.loads(data))
        return 200, {}, b"{}"
    return 200, {}, b"{}"


storytel._request = push_fake


def set_sync(on):
    from vibb import sysinfo
    sysinfo.update_settings({"storytel_sync": 1 if on else 0})


# 1. toggle OFF: the mirror does nothing, and touches no network
set_sync(False)
daemon._STORYTEL_MIRRORED.clear()
daemon._storytel_mirror_tick()
assert PUSHED == [], "sync off must push nothing"
assert storytel.outbox_pending() == 0
print("1. the toggle off means zero network OK")

# 2. toggle ON: both books' positions are pushed once, in milliseconds,
#    with the stable device id
set_sync(True)
daemon._STORYTEL_MIRRORED.clear()
daemon._storytel_mirror_tick()
by_id = {p["consumableId"]: p for p in PUSHED}
assert by_id["111"]["position"] == 640000, by_id["111"]
assert by_id["222"]["position"] == 120000, by_id["222"]
assert all(p["deviceId"] == storytel.device_id() for p in PUSHED)
assert storytel.outbox_pending() == 0, "a successful push drains the queue"
print("2. both positions push once, in ms, with the device id OK")

# 3. an unchanged position on the next tick pushes NOTHING — the dedup
#    that stops a steadily-playing book POSTing every tick
PUSHED.clear()
daemon._storytel_mirror_tick()
assert PUSHED == [], "an unchanged position must not re-push"
print("3. an unchanged position does not re-push OK")

# 4. a moved position pushes again, and only the one that moved
bookmarks.save_state(key, "/c/111.mp3", 660.0, "111", 700.0)
PUSHED.clear()
daemon._storytel_mirror_tick()
assert [p["consumableId"] for p in PUSHED] == ["111"], PUSHED
assert PUSHED[0]["position"] == 660000
print("4. only a book whose position moved is pushed again OK")

# 5. offline: the position queues, nothing is lost, nothing raises
def boom(*a, **k):
    raise OSError("offline")


storytel._request = boom
bookmarks.save_state(key, "/c/222.mp3", 500.0, "222", 700.0)
daemon._storytel_mirror_tick()          # must not raise
assert storytel.outbox_pending() >= 1, "an offline position must queue"
print("5. offline, the position queues and nothing raises OK")

# 6. back online, the queue drains
storytel._request = push_fake
daemon._storytel_mirror_tick()
assert storytel.outbox_pending() == 0, "the queue must drain when online"
print("6. back online, the queued positions drain OK")

# 7. A BOOK HEARD TO THE END IS REPORTED AS FINISHED. This is the one
#    that bit in the field (2026-08-16): "Kokosbananas og tomattorsken"
#    played out on the box, the player rolled on to the next episode
#    untouched, and the account still said 2:20 — the position we had
#    mirrored mid-listen. save_state used to express completion by
#    DELETING the record, and the mirror only walks records that exist,
#    so the one moment worth reporting was the one moment that pushed
#    nothing. Storytel derives its own state from position (our small
#    pushes are what flipped books from WILL_CONSUME to CONSUMING), so
#    the end of the book is what marks it CONSUMED.
PUSHED.clear()
bookmarks.save_state(key, "/c/111.mp3", 695.0, "111", 700.0)   # rolled over
daemon._storytel_mirror_tick()
assert [p["consumableId"] for p in PUSHED] == ["111"], \
    f"a finished book must be reported, not silently dropped: {PUSHED}"
assert PUSHED[0]["position"] == 700000, \
    ("the DURATION is what gets sent, not the 695s we happened to stop at — "
     f"the threshold accepts anything within 20s of the end: {PUSHED[0]}")
print("7. a book heard to the end is pushed as finished OK")

# 8. ...and it still resumes from the start, which is why the record was
#    deleted in the first place. The tombstone must not become a resume.
assert bookmarks.episode_pos(bookmarks.load_state(key), "111",
                             "/c/111.mp3") == 0.0, \
    "a finished episode must re-tap from the start, not from its last second"
print("8. a finished episode still starts fresh on the next tap OK")

# 8b. A FINISHED BOOK MUST LAND ON STORYTEL'S OWN DURATION. The app's
#     "N% fullført" is computed against THEIR number, so reporting mpv's
#     measurement of the file we downloaded left books stuck at 96-98%
#     forever — visible on the owner's progress screen (field 2026-08-18).
#     The shelf we already persist carries the authoritative value.
storytel.write_shelf(TARGET, "Kokosbananas", [
    {"consumable_id": "111", "title": "En", "order": 1,
     "duration_ms": 900_000},          # Storytel says 900s
])
PUSHED.clear()
bookmarks.save_state(key, "/c/111.mp3", 695.0, "111", 700.0)  # mpv says 700s
daemon._storytel_mirror_tick()
assert [p["consumableId"] for p in PUSHED] == ["111"], PUSHED
assert PUSHED[0]["position"] == 900_000, \
    ("a finished book must be reported at Storytel's own duration, not "
     f"mpv's view of our download: {PUSHED[0]}")
print("8c. a finished book lands on Storytel's own duration OK")

# 9. THE END OF A QUEUE MUST NOT EAT THE COMPLETION. player.py clears the
#    state when a queue finishes by itself — precisely when the last
#    episode's completion is freshest and the 60s mirror is least likely
#    to have seen it. Removing the file there loses the very fact the
#    mirror exists to report, so `done` records survive a clear.
PUSHED.clear()
bookmarks.save_state(key, "/c/222.mp3", 698.0, "222", 700.0)   # last one out
bookmarks.clear_state(key)                                     # queue over
st_after = bookmarks.load_state(key) or {}
assert (st_after.get("episodes") or {}).get("222", {}).get("done"), \
    "clearing a finished queue threw away the completion before it was sent"
assert not st_after.get("url") and not st_after.get("id"), \
    "the resume bookmark itself must still be gone — clear_state must clear"
daemon._storytel_mirror_tick()
assert [p["consumableId"] for p in PUSHED] == ["222"], \
    f"the last episode of a queue must still reach Storytel: {PUSHED}"
print("9. a completion survives the end-of-queue clear OK")

# 10. and with nothing finished, clear_state still removes the file
#     outright — an unfinished queue leaves no trace, as before. A key of
#     its own: the one above is full of completions by now, which is
#     exactly the case this pin is NOT about.
fresh = key + "-unfinished"
bookmarks.save_state(fresh, "/c/333.mp3", 30.0, "333", 700.0)
bookmarks.clear_state(fresh)
assert bookmarks.load_state(fresh) is None, \
    "with no completions to keep, clear_state must leave nothing behind"
print("10. with nothing finished, the state file is removed as before OK")

print("\nSTORYTEL BOOKMARK SYNC OK — one-way out, silent when unchanged, "
      "an offline stretch is queued, and a book heard to the end is "
      "reported as finished.")
