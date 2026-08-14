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

print("\nSTORYTEL BOOKMARK SYNC OK — one-way out, silent when unchanged, "
      "and an offline stretch is queued, never lost.")
