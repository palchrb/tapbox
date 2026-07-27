#!/usr/bin/env python3
"""Gate metadata + cover art for uploaded audio.

Without this, an uploaded audiobook shows as "01-track_03_final" on a
240px screen and has no artwork — technically playable, practically
unusable for a kid. So the uploader reads the file's embedded tags with
ffprobe (ffmpeg is already an install.sh dependency — no new package)
and records them in a per-collection sidecar.

Two deliberate choices under test:
- Embedded cover art is extracted to `cover.jpg`, the exact name
  collection_image() already looks for — so artwork works with NO change
  to content.py. A parent-uploaded cover must WIN over the embedded one.
- Track ordering only overrides filename order when EVERY file has a
  track number; a partial set would interleave worse than filenames do.

A folder with no sidecar (copied on by scp, or uploaded before this
existed) must behave exactly as before."""
import json
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
MEDIA = os.path.join(TMP, "media")
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_MEDIA"] = MEDIA
os.environ["TAPBOX_LIBRARY"] = os.path.join(TMP, "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from tapbox import content  # noqa: E402

COLL = os.path.join(MEDIA, "Ronja")
os.makedirs(COLL)

# ffmpeg isn't in this container, so stub the two subprocess calls with
# something that mimics ffprobe/ffmpeg's contract (json on stdout / a
# written file + returncode).
TAGS = {
    "a.mp3": {"title": "Kapittel to", "album": "Ronja", "track": "2/3"},
    "b.mp3": {"title": "Kapittel en", "album": "Ronja", "track": "1/3"},
    "c.mp3": {"album": "Ronja"},  # no title, no track
}
EMBEDDED_ART = {"a.mp3"}
calls = []


def fake_run(cmd, **kw):
    calls.append(cmd[0])
    if cmd[0] == "ffprobe":
        name = os.path.basename(cmd[-1])
        out = json.dumps({"format": {"tags": TAGS.get(name, {})}})
        return types.SimpleNamespace(stdout=out, returncode=0)
    if cmd[0] == "ffmpeg":
        src, dest = os.path.basename(cmd[cmd.index("-i") + 1]), cmd[-1]
        if src in EMBEDDED_ART:
            with open(dest, "wb") as f:
                f.write(b"\xff\xd8\xff" + b"jpegbytes")
            return types.SimpleNamespace(returncode=0)
        return types.SimpleNamespace(returncode=1)
    raise AssertionError(cmd)


daemon.subprocess = types.SimpleNamespace(
    run=fake_run, SubprocessError=Exception,
    TimeoutExpired=Exception)

for n in ("a.mp3", "b.mp3", "c.mp3"):
    with open(os.path.join(COLL, n), "wb") as f:
        f.write(b"ID3" + os.urandom(200))

# 1. probing records the embedded tags in the sidecar
tags = daemon._media_note_meta(COLL, "a.mp3", os.path.join(COLL, "a.mp3"))
assert tags["title"] == "Kapittel to" and tags["track"] == 2, tags
daemon._media_note_meta(COLL, "b.mp3", os.path.join(COLL, "b.mp3"))
daemon._media_note_meta(COLL, "c.mp3", os.path.join(COLL, "c.mp3"))
side = json.load(open(os.path.join(COLL, daemon.MEDIA_META)))
assert side["a.mp3"]["track"] == 2 and side["b.mp3"]["track"] == 1, side
assert "title" not in side.get("c.mp3", {}), side
print("1. ffprobe tags recorded in the sidecar ('2/3' -> 2) OK")

# 2. the menus now show TITLES, not filenames
entries = content.expand_entries(COLL)
by_id = {e["id"]: e["title"] for e in entries}
assert by_id["a.mp3"] == "Kapittel to", by_id
assert by_id["c.mp3"] == "c", "an untagged file falls back to its filename"
print("2. titles come from tags, untagged files fall back to filename OK")

# 3. ordering: NOT all files have a track number here, so filename order
#    must stand — a partial set interleaves worse than filenames
assert [e["id"] for e in entries] == ["a.mp3", "b.mp3", "c.mp3"], entries
print("3. partial track numbers do NOT reorder (filenames win) OK")

# 3b. give every file a number -> track order takes over
side["c.mp3"] = {"title": "Kapittel tre", "track": 3}
with open(os.path.join(COLL, daemon.MEDIA_META), "w") as f:
    json.dump(side, f)
entries = content.expand_entries(COLL)
assert [e["id"] for e in entries] == ["b.mp3", "a.mp3", "c.mp3"], entries
assert [e["title"] for e in entries] == ["Kapittel en", "Kapittel to",
                                         "Kapittel tre"], entries
print("3b. a complete track set reorders the chapters correctly OK")

# 4. embedded art is extracted to cover.jpg — the name collection_image
#    already looks for, so artwork needs NO content.py change
assert daemon._media_extract_cover(os.path.join(COLL, "a.mp3"), COLL) is True
cover = os.path.join(COLL, "cover.jpg")
assert os.path.exists(cover)
assert content.collection_image(COLL) == cover, content.collection_image(COLL)
assert all(e["image"] == cover for e in content.expand_entries(COLL))
print("4. embedded art -> cover.jpg, picked up as the collection image OK")

# 5. a parent-uploaded cover must WIN over the embedded one
with open(cover, "wb") as f:
    f.write(b"\xff\xd8\xffPARENT")
assert daemon._media_extract_cover(os.path.join(COLL, "a.mp3"), COLL) is False
assert open(cover, "rb").read().endswith(b"PARENT"), \
    "an existing cover must never be overwritten by embedded art"
print("5. an existing cover.jpg is never overwritten OK")

# 6. NO sidecar (scp'd folder, or uploaded before this existed) behaves
#    exactly as before: filenames as titles, filename order
plain = os.path.join(MEDIA, "Gamle")
os.makedirs(plain)
for n in ("02.mp3", "01.mp3"):
    with open(os.path.join(plain, n), "wb") as f:
        f.write(b"ID3")
entries = content.expand_entries(plain)
assert [e["id"] for e in entries] == ["01.mp3", "02.mp3"], entries
assert [e["title"] for e in entries] == ["01", "02"], entries
print("6. a folder with no sidecar keeps the old behaviour OK")

# 7. ffprobe missing/failing must not break the upload — it just means
#    filenames, which is what happened before this feature
def boom(cmd, **kw):
    raise OSError("no ffprobe")


daemon.subprocess = types.SimpleNamespace(
    run=boom, SubprocessError=Exception, TimeoutExpired=Exception)
assert daemon._media_probe(os.path.join(COLL, "a.mp3")) == {}
assert daemon._media_extract_cover(os.path.join(COLL, "a.mp3"), plain) is False
print("7. a box without ffmpeg degrades to filenames, no crash OK")

print("\nall media_metadata checks passed")
