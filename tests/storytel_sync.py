#!/usr/bin/env python3
"""Downloading a series: the sweeper runs storytel.py, and sync fetches.

The download path reuses the existing sweeper wholesale — nice-19,
one-core, busy-yield, SweepYield — and only swaps WHICH module the
subprocess runs. The pieces that must hold:

  - _sync_one builds its command with storytel.py, not content.py, when
    handed mod= (and still defaults to content.py, so the podcast path
    and its sweep_yield test are untouched);
  - storytel.sync writes the FULL book listing but downloads only the
    requested prefix, skips locked/geo books (they 403), resumes a
    .part with a re-minted url, and stops on a low disk rather than
    filling the card;
  - a storytel entry with cache 0 is coerced to -1: download-only
    content 'in the library' means downloaded, and cache 0 would be an
    entry that plays nothing.

No network, no real subprocess: bookshelf/asset_url/_download and
Popen are faked."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
CACHE = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = CACHE
os.environ["VIBB_STATE"] = tempfile.mkdtemp()

from vibb import storytel, content, library  # noqa: E402

TARGET = "storytel:series:26175"
SHELF = {"items": {
    "a": {"model": {"id": "111", "title": "En", "kidsBook": True,
                    "seriesInfo": {"id": "26175", "name": "Kokosbananas",
                                   "orderInSeries": 1},
                    "formats": [{"type": "abook", "id": "111",
                                 "durationInMilliseconds": 700000,
                                 "cover": {"url": "http://c/1.jpg"},
                                 "isLockedContent": False}]}},
    "b": {"model": {"id": "222", "title": "To", "kidsBook": True,
                    "seriesInfo": {"id": "26175", "name": "Kokosbananas",
                                   "orderInSeries": 2},
                    "formats": [{"type": "abook", "id": "222",
                                 "durationInMilliseconds": 700000,
                                 "isLockedContent": False}]}},
    "c": {"model": {"id": "333", "title": "Tre (låst)", "kidsBook": True,
                    "seriesInfo": {"id": "26175", "name": "Kokosbananas",
                                   "orderInSeries": 3},
                    "formats": [{"type": "abook", "id": "333",
                                 "durationInMilliseconds": 700000,
                                 "isLockedContent": True}]}},
}}
DOWNLOADS = []


def fake_download(url, dest, timeout=120, resume=False):
    DOWNLOADS.append({"url": url, "dest": os.path.basename(dest),
                      "resume": resume})
    with open(dest, "wb") as f:
        f.write(b"\xff\xfb audio")


storytel.bookshelf = lambda: SHELF


def fake_asset(cid):
    # book 333 is genuinely out of reach: the server refuses a url. The
    # 'locked' FLAG on the shelf is not trusted — only a real failure
    # skips a book (field 2026-08-15: locked-flagged books play fine).
    if cid == "333":
        raise OSError("403 forbidden")
    return f"https://fastly/{cid}?token=SIG"


storytel.asset_url = fake_asset
storytel._download = fake_download   # sync uses its OWN inlined download now

# 1. sync attempts EVERY book regardless of the locked flag, downloads
#    the ones the server allows, skips only a genuine failure, and writes
#    a shelf.json listing all three
storytel.sync(TARGET, -1)
d = storytel.cache_dir(TARGET)
assert os.path.exists(os.path.join(d, "111.mp3"))
assert os.path.exists(os.path.join(d, "222.mp3"))
assert not os.path.exists(os.path.join(d, "333.mp3")), \
    "a book the server refuses is skipped — but only on a real failure"
shelf = storytel.read_shelf(TARGET)
assert [b["consumable_id"] for b in shelf["books"]] == ["111", "222", "333"], \
    "the listing keeps every book, downloaded or not"
print("1. sync attempts every book and skips only a real failure OK")

# 1b. the locked FLAG alone does not skip a book: 333 was attempted (its
#     asset_url was called) and only failed because the server refused
attempted = []
_real_asset = storytel.asset_url
storytel.asset_url = lambda cid: attempted.append(cid) or _real_asset(cid)
import shutil as _sh  # noqa: E402
_sh.rmtree(d)
storytel.sync(TARGET, -1)
assert "333" in attempted, "a locked-flagged book must still be ATTEMPTED"
print("1b. a locked flag does not pre-skip — the server decides OK")
storytel.asset_url = fake_asset

# 2. the signed url is re-minted per book, and the download asks to resume
book_dls = [x for x in DOWNLOADS if x["dest"].endswith(".mp3")]
assert {x["url"] for x in book_dls} == {"https://fastly/111?token=SIG",
                                        "https://fastly/222?token=SIG"}
assert all(x["resume"] for x in book_dls), "a book download must be resumable"
print("2. each book re-mints its url and downloads resumably OK")

# 3. a second sync is incremental — already-downloaded books are skipped
DOWNLOADS.clear()
storytel.sync(TARGET, -1)
assert not any(x["dest"].endswith(".mp3") for x in DOWNLOADS), \
    "already-downloaded books must not re-fetch"
print("3. a re-sync skips books already on disk OK")

# 4. count N downloads only the first N in reading order
import shutil  # noqa: E402
shutil.rmtree(d)
DOWNLOADS.clear()
storytel.sync(TARGET, 1)
assert os.path.exists(os.path.join(d, "111.mp3"))
assert not os.path.exists(os.path.join(d, "222.mp3")), "count=1 must stop after book 1"
print("4. a count limits the download to the first N books OK")

# 5. a low disk stops the sync cleanly, before the next book, without
#    corrupting anything
shutil.rmtree(d)
DOWNLOADS.clear()
storytel._free_bytes = lambda p: 1000   # below the floor
storytel.sync(TARGET, -1)
assert not any(x["dest"].endswith(".mp3") for x in DOWNLOADS), \
    "a full disk must stop before downloading"
assert storytel.read_shelf(TARGET), "but the listing is still written"
print("5. a low disk stops before downloading, listing still written OK")
storytel._free_bytes = lambda p: 1 << 40

# 6. _sync_one runs storytel.py when handed mod=, content.py otherwise —
#    the podcast path and its sweep_yield test stay untouched
CMDS = []


class FakePopen:
    returncode = 0

    def __init__(self, cmd, **k):
        CMDS.append(cmd)

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


library.subprocess.Popen = FakePopen
library._busy = lambda: False
library._sync_one(["sync", TARGET, "-1"], mod=storytel.__file__)
library._sync_one(["sync", "some-slug", "5", "podcast"])
assert storytel.__file__ in CMDS[0], "mod= must run storytel.py"
assert content.__file__ in CMDS[1], "the default must still be content.py"
print("6. _sync_one runs the right module for each source OK")

# 7. a storytel entry with cache 0 is coerced to -1 (download-only: 'in
#    the library' means downloaded), while a podcast keeps cache 0
lib = library.normalize_library({"sections": [{"name": "Lyd", "entries": [
    {"name": "Kokos", "target": TARGET, "cache": 0},
    {"name": "Pod", "target": "https://radio.nrk.no/podkast/x", "cache": 0},
]}]})
by_name = {e["name"]: e for e in lib["sections"][0]["entries"]}
assert by_name["Kokos"]["cache"] == -1, "storytel cache 0 must become -1"
assert by_name["Pod"]["cache"] == 0, "a podcast keeps cache 0"
print("7. a storytel entry with cache 0 is coerced to download-all OK")

print("\nSTORYTEL SYNC OK — the sweeper runs storytel.py, sync downloads a "
      "prefix and lists the rest, and a full disk stops it cold.")
