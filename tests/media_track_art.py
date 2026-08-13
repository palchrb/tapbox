#!/usr/bin/env python3
"""Per-track embedded art for local collections (QA-reviewed
2026-08-13). iTunes buys / CD rips carry the cover inside each file;
one cover.jpg per folder was wrong for loose singles. Pins: extraction
once-ever with a .none marker for art-less files (the sweep must never
re-probe), NEVER rewriting an existing jpg (mtime keys the UI thumbs),
per-track image in the expansion with folder-cover fallback, the heal
pass filling gaps + pruning orphans idempotently, and the delete
helper cleaning art (the .art dir made collections undeletable — QA
blocker). ffprobe/ffmpeg are FAKED via subprocess.run — the flags
themselves are field-verified on the box, which ships ffmpeg."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP

from vibb import content  # noqa: E402

FOLDER = tempfile.mkdtemp()
runs = []


class FakeProc:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def fake_run(cmd, **kw):
    runs.append(cmd[0])
    src = next(a for a in cmd if a.startswith("/") or a.startswith(FOLDER))
    has_pic = "withart" in os.path.basename(src) or cmd[0] == "ffmpeg"
    if cmd[0] == "ffprobe":
        streams = [{"codec_type": "audio"}]
        if "withart" in os.path.basename(src):
            streams.append({"codec_type": "video",
                            "disposition": {"attached_pic": 1}})
        return FakeProc(0, json.dumps({"streams": streams}))
    if cmd[0] == "ffmpeg":  # extraction: "write" the jpg (last arg)
        with open(cmd[-1], "wb") as f:
            f.write(b"\xff\xd8fakejpeg")
        return FakeProc(0)
    raise AssertionError(cmd)


content.subprocess.run = fake_run

for name in ("01 withart.mp3", "02 noart.mp3", "03 withart.m4b"):
    with open(os.path.join(FOLDER, name), "wb") as f:
        f.write(b"audio")

# 1. extraction: art for the tagged files, .none for the bare one —
#    and NO ffmpeg spawn for the file the probe saw no picture in
assert content.extract_track_art(FOLDER, "01 withart.mp3") is True
assert content.extract_track_art(FOLDER, "02 noart.mp3") is False
assert os.path.exists(content.track_art_path(FOLDER, "01 withart.mp3"))
assert os.path.exists(
    content.track_art_path(FOLDER, "02 noart.mp3")[:-4] + ".none")
assert runs == ["ffprobe", "ffmpeg", "ffprobe"], runs
print("1. extract: art out, .none marker, no wasted ffmpeg OK")

# 2. once-ever: second calls do NOTHING (no probe, no rewrite)
art1 = content.track_art_path(FOLDER, "01 withart.mp3")
os.utime(art1, (1000, 1000))
runs.clear()
assert content.extract_track_art(FOLDER, "01 withart.mp3") is True
assert content.extract_track_art(FOLDER, "02 noart.mp3") is False
assert runs == [], "existing art/marker must skip all subprocesses"
assert os.path.getmtime(art1) == 1000, \
    "an existing jpg must NEVER be rewritten (mtime keys the UI thumbs)"
print("2. once-ever: no re-probe, no rewrite, mtime untouched OK")

# 3. expansion: per-track art wins, folder cover is the fallback
with open(os.path.join(FOLDER, "cover.jpg"), "wb") as f:
    f.write(b"\xff\xd8cover")
eps = content.expand_entries(FOLDER)
by_id = {e["id"]: e["image"] for e in eps}
assert by_id["01 withart.mp3"] == art1
assert by_id["02 noart.mp3"] == os.path.join(FOLDER, "cover.jpg")
assert [e["id"] for e in eps] == sorted(by_id), "order untouched"
print("3. expansion: own art per track, cover fallback OK")

# 4. heal: fills the remaining gap, prunes orphans, then goes idle
assert content.folder_art_pending(FOLDER) is True   # 03 still missing
orphan = os.path.join(FOLDER, content.ART_DIR, "deleted.mp3.jpg")
with open(orphan, "wb") as f:
    f.write(b"x")
runs.clear()
content.heal_folder_art(FOLDER)
assert os.path.exists(content.track_art_path(FOLDER, "03 withart.m4b"))
assert not os.path.exists(orphan), "art for deleted files must prune"
assert content.folder_art_pending(FOLDER) is False
runs.clear()
content.heal_folder_art(FOLDER)
assert runs == [], "a healed folder must cost zero subprocesses"
print("4. heal fills gaps, prunes orphans, second run free OK")

# 5. drop_track_art removes both art and marker (single-file delete)
content.drop_track_art(FOLDER, "01 withart.mp3")
content.drop_track_art(FOLDER, "02 noart.mp3")
assert not os.path.exists(art1)
assert not os.path.exists(
    content.track_art_path(FOLDER, "02 noart.mp3")[:-4] + ".none")
print("5. drop_track_art: jpg and marker both gone OK")

print("\nMEDIA TRACK ART OK — every song wears its own cover, "
      "extracted once, healed nightly, deleted cleanly.")
