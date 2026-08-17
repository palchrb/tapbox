#!/usr/bin/env python3
"""The load-bearing half of backup: WHICH files are captured, and that the
restore re-applies them atomically with the right permissions.

No restic, no network — collect() builds a staging tree and apply_tree()
writes it back, both pure filesystem. The restic/rclone wrappers are thin and
covered separately (backup_restic.py).

Pins the invariants that would quietly corrupt a restore if they broke:
  1. the whitelist takes config + secret + progress, and NOTHING else —
     api-token, rclone.conf, restic-pass and transient STATE markers stay out;
  2. arbitrary bookmark keys (sha1 names) ARE taken — the whole point;
  3. a round-trip is byte-identical and secret files land 0600;
  4. a backup whose schema is newer than this code is refused;
  5. a torn JSON member aborts the whole restore having written nothing.
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))

ETC = tempfile.mkdtemp()
STATE = tempfile.mkdtemp()
ART = tempfile.mkdtemp()
GO = tempfile.mkdtemp()
os.environ["VIBB_ETC"] = ETC
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_ART"] = ART
os.environ["VIBB_GO_CONFIG"] = os.path.join(GO, "config.yml")

from vibb import backup  # noqa: E402


def w(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


# --- a plausible box on disk ------------------------------------------------
w(os.path.join(ETC, "library.json"), json.dumps({"version": 1, "sections": []}))
w(os.path.join(ETC, "settings.json"), json.dumps({"screen_brightness": 7}))
w(os.path.join(ETC, "cards.json"), json.dumps({"04AABB": "storytel:book:1"}))
w(os.path.join(ETC, "rfid.conf"), "MODE=poll\n")
w(os.path.join(ETC, "bt-headset"), "AA:BB:CC:DD:EE:FF\n")
w(os.path.join(ETC, "storytel.json"), json.dumps({"email": "a@b.c",
                                                  "password": "s3cret"}), 0o600)
w(os.path.join(ETC, "spotify-api.json"), json.dumps({"client_id": "x"}), 0o600)
w(os.path.join(ETC, "api-token"), "ABCD1234ABCD1234\n", 0o600)   # must NOT ship
w(os.path.join(ETC, "rclone.conf"), "[myremote]\n", 0o600)       # must NOT ship
w(os.path.join(ETC, "restic-pass"), "hunter2\n", 0o600)          # must NOT ship
w(os.path.join(ETC, "backup.json"),
  json.dumps({"repo": "rclone:myremote:vibb-backup"}), 0o600)    # must NOT ship
w(os.path.join(GO, "config.yml"), "device_name: vibb\n")
w(os.path.join(GO, "credentials.json"), json.dumps({"u": "spotifyuser"}), 0o600)
w(os.path.join(GO, "state.json"), json.dumps({"credentials": {"username": "u"}}))
w(os.path.join(ART, "section-eventyr.jpg"), "JPEGDATA")

# STATE: a real bookmark (arbitrary sha1 key), the wanted singletons, and
# transient markers that must be dropped.
BM_KEY = "a1b2c3d4e5f6"   # sha1(target)[:12] — an arbitrary bookmark name
w(os.path.join(STATE, BM_KEY + ".json"),
  json.dumps({"url": "/x", "pos": 90.0, "id": "ep1", "episodes": {}}))
w(os.path.join(STATE, "spotify-bm-deadbeef0000.json"), json.dumps({"position": 5}))
w(os.path.join(STATE, "storytel-outbox.json"), json.dumps({}))
w(os.path.join(STATE, "storytel-device.json"), json.dumps({"device_id": "U"}))
w(os.path.join(STATE, "last-play.json"), json.dumps({"target": "x"}))
w(os.path.join(STATE, "volume.json"), json.dumps({"volume": 40}))
# transient — must be excluded
w(os.path.join(STATE, "now-playing.json"), json.dumps({"url": "x"}))
w(os.path.join(STATE, "now-queue.json"), json.dumps({}))
w(os.path.join(STATE, "sonos.json"), json.dumps({}))
w(os.path.join(STATE, "storytel-shelf-raw.json"), json.dumps({}))
w(os.path.join(STATE, "bt-quiet"), "")          # non-.json marker

# --- 1. collect the set -----------------------------------------------------
staging = tempfile.mkdtemp()
manifest = backup.collect(staging)
paths = {e["path"] for e in manifest["files"]}

took = lambda p: p in paths  # noqa: E731
assert took(os.path.join(ETC, "library.json")), "library must be backed up"
assert took(os.path.join(ETC, "storytel.json")), "storytel creds must ship"
assert took(os.path.join(GO, "credentials.json")), "spotify creds must ship"
assert took(os.path.join(ART, "section-eventyr.jpg")), "section logo must ship"
assert took(os.path.join(STATE, BM_KEY + ".json")), \
    "an arbitrary bookmark key must be captured — that is the whole point"
assert took(os.path.join(STATE, "storytel-device.json")), "device id must ship"

assert not took(os.path.join(ETC, "api-token")), \
    "api-token must NEVER ship (a restored box mints a fresh one)"
# The backend's own credentials cannot be restored FROM the backend they
# unlock (chicken-and-egg), and are re-entered at setup on a new box.
assert not took(os.path.join(ETC, "rclone.conf")), "backend key must not ship"
assert not took(os.path.join(ETC, "restic-pass")), "repo pass must not ship"
assert not took(os.path.join(ETC, "backup.json")), "repo pointer must not ship"
assert not took(os.path.join(STATE, "now-playing.json")), "transient excluded"
assert not took(os.path.join(STATE, "now-queue.json")), "transient excluded"
assert not took(os.path.join(STATE, "sonos.json")), "transient excluded"
assert not took(os.path.join(STATE, "storytel-shelf-raw.json")), "cache excluded"
assert not any(p.endswith("bt-quiet") for p in paths), "non-json marker excluded"
assert manifest["token_included"] is False
assert set(manifest["tiers"]) == {"config", "secret", "progress"}
print("1. the whitelist takes config+secret+progress and nothing else OK")

# tier tagging: secrets tagged so restore can force 0600
by_path = {e["path"]: e for e in manifest["files"]}
assert by_path[os.path.join(ETC, "storytel.json")]["tier"] == "secret"
assert by_path[os.path.join(GO, "credentials.json")]["tier"] == "secret"
assert by_path[os.path.join(ETC, "library.json")]["tier"] == "config"
assert by_path[os.path.join(STATE, BM_KEY + ".json")]["tier"] == "progress"
print("2. every file is tagged with its tier OK")

# --- 3. round-trip: wipe originals, apply, verify ---------------------------
originals = {}
for e in manifest["files"]:
    with open(e["path"], "rb") as f:
        originals[e["path"]] = f.read()
    os.remove(e["path"])                 # simulate the dead card

backup.apply_tree(staging)

for path, want in originals.items():
    with open(path, "rb") as f:
        assert f.read() == want, f"{path} did not round-trip byte-for-byte"
# secrets forced 0600 regardless of their recorded mode
for secret in (os.path.join(ETC, "storytel.json"),
               os.path.join(ETC, "spotify-api.json"),
               os.path.join(GO, "credentials.json")):
    assert (os.stat(secret).st_mode & 0o777) == 0o600, \
        f"{secret} must be restored 0600"
# no stray .vibbrestore.tmp left behind
assert not any(n.endswith(backup.RESTORE_TMP_SUFFIX)
               for n in os.listdir(ETC)), "restore tmp not cleaned up"
print("3. a round-trip is byte-identical and secrets land 0600 OK")

# --- 4. a too-new schema is refused -----------------------------------------
future = tempfile.mkdtemp()
os.makedirs(os.path.join(future, "files"))
with open(os.path.join(future, "manifest.json"), "w") as f:
    json.dump({"format": backup.MANIFEST_FORMAT, "schema": backup.SCHEMA + 1,
               "files": []}, f)
try:
    backup.apply_tree(future)
    assert False, "a newer-schema backup must be refused"
except ValueError as e:
    assert "newer" in str(e)
print("4. a backup newer than this code is refused OK")

# --- 5. a torn JSON member aborts, writing nothing --------------------------
torn = tempfile.mkdtemp()
dest = os.path.join(ETC, "library.json")
before = open(dest, "rb").read()
fdir = os.path.join(torn, "files", ETC.lstrip("/"))
os.makedirs(fdir, exist_ok=True)
with open(os.path.join(fdir, "library.json"), "w") as f:
    f.write("{ this is not json ")
with open(os.path.join(torn, "manifest.json"), "w") as f:
    json.dump({"format": backup.MANIFEST_FORMAT, "schema": backup.SCHEMA,
               "files": [{"path": dest, "tier": "config", "mode": "0o644"}]}, f)
try:
    backup.apply_tree(torn)
    assert False, "a torn json member must abort the restore"
except ValueError:
    pass
assert open(dest, "rb").read() == before, \
    "a failed restore must leave the live file untouched"
assert not os.path.exists(dest + backup.RESTORE_TMP_SUFFIX), \
    "the staged tmp must be cleaned up on abort"
print("5. a torn JSON member aborts the restore, writing nothing OK")

print("\nBACKUP ROUNDTRIP OK — right files, atomic restore, secrets 0600, "
      "and a bad bundle changes nothing.")
