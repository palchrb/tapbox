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
w(os.path.join(ETC, "library.json"), json.dumps({"version": 1, "sections": [
    {"id": "eventyr", "name": "Eventyr", "entries": [],
     # absolute path, exactly as POST /library/section-logo stores it
     "image": os.path.join(ART, "section-eventyr.jpg")}]}))
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

# 3b. A section logo and the library entry that POINTS at it must survive
#     together. library.json stores the logo's ABSOLUTE path (the upload
#     route writes sec["image"] = path), so the reference only stays valid
#     because both are restored to the same absolute locations — ART_DIR is
#     /var/lib/vibb/art on every box. If either the glob or the path guard
#     ever drops section art, the carousel comes back with dead references
#     and nobody notices until a parent looks at the home screen.
lib_after = json.load(open(os.path.join(ETC, "library.json")))
ref = lib_after["sections"][0]["image"]
assert ref == os.path.join(ART, "section-eventyr.jpg"), ref
assert os.path.isfile(ref), \
    "the library points at a section logo the restore did not bring back"
assert open(ref, "rb").read() == b"JPEGDATA", "the logo came back corrupt"
print("3b. a section logo and the library reference to it survive together OK")

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

# --- 6. A HOSTILE MANIFEST CANNOT WRITE OUTSIDE THE BOX'S OWN CONFIG -------
#     vibb-daemon runs as ROOT (install.sh gives it no User=), and the
#     manifest decides where bytes land. Without validation, one entry of
#     "/etc/systemd/system/x.service" makes restore an arbitrary root-owned
#     overwrite. The repo is encrypted, but the password sits on the same SD
#     card — so this is defence in depth on the one path that writes over
#     live secrets (QA 2026-08-17).
outside = os.path.join(tempfile.mkdtemp(), "victim.conf")
with open(outside, "w") as f:
    f.write("ORIGINAL")

for bad_path in (outside,                                  # absolute, elsewhere
                 os.path.join(ETC, "..", "escape.json"),   # ..-escape
                 "relative.json",                          # not absolute
                 ""):
    ev = tempfile.mkdtemp()
    d = os.path.join(ev, "files", bad_path.lstrip("/") or "x")
    os.makedirs(os.path.dirname(d), exist_ok=True)
    with open(d, "w") as f:
        f.write("PWNED")
    with open(os.path.join(ev, "manifest.json"), "w") as f:
        json.dump({"format": backup.MANIFEST_FORMAT, "schema": backup.SCHEMA,
                   "files": [{"path": bad_path, "tier": "config",
                              "mode": "0o644"}]}, f)
    try:
        backup.apply_tree(ev)
        assert False, f"a manifest writing to {bad_path!r} must be refused"
    except ValueError:
        pass
assert open(outside).read() == "ORIGINAL", \
    "a hostile manifest overwrote a file outside the box's config"
print("6. a manifest pointing outside the box's own config is refused OK")

# 6b. AND the dangerous neighbours INSIDE /etc/vibb. Validating by directory
#     was not enough: api-token is the box's own credential (a restore could
#     set it to a value the attacker knows), and extras/ holds scripts ui.py
#     lists and runs AS ROOT from the screen. Neither is in the backup set,
#     so neither may be a restore target (QA 2026-08-17).
for inside in (os.path.join(ETC, "api-token"),
               os.path.join(ETC, "extras", "pwn.sh"),
               os.path.join(ETC, "rclone.conf"),
               os.path.join(ETC, "restic-pass"),
               os.path.join(STATE, "now-playing.json")):
    ev = tempfile.mkdtemp()
    d = os.path.join(ev, "files", inside.lstrip("/"))
    os.makedirs(os.path.dirname(d), exist_ok=True)
    with open(d, "w") as f:
        f.write("PWNED")
    with open(os.path.join(ev, "manifest.json"), "w") as f:
        json.dump({"format": backup.MANIFEST_FORMAT, "schema": backup.SCHEMA,
                   "files": [{"path": inside, "tier": "config",
                              "mode": "0o755"}]}, f)
    try:
        backup.apply_tree(ev)
        assert False, f"a restore must never write {inside}"
    except ValueError:
        pass
assert open(os.path.join(ETC, "api-token")).read().startswith("ABCD"), \
    "the box token must be untouched by a restore"
print("6b. api-token, extras/ and the backend keys are not restore targets OK")

# 7. and it cannot ask for a setuid bit either — the mode is masked.
ev = tempfile.mkdtemp()
d = os.path.join(ev, "files", ETC.lstrip("/"), "cards.json")
os.makedirs(os.path.dirname(d), exist_ok=True)
with open(d, "w") as f:
    f.write("{}")
with open(os.path.join(ev, "manifest.json"), "w") as f:
    json.dump({"format": backup.MANIFEST_FORMAT, "schema": backup.SCHEMA,
               "files": [{"path": os.path.join(ETC, "cards.json"),
                          "tier": "config", "mode": "0o4755"}]}, f)
backup.apply_tree(ev)
mode = os.stat(os.path.join(ETC, "cards.json")).st_mode
assert not (mode & 0o4000), "restore must never grant setuid"
print("7. a manifest asking for setuid gets it masked off OK")

# --- 8. the JWT is not in the set ------------------------------------------
#     storytel.py writes storytel-session.json 0600 as a bearer token. The
#     progress tier restores 0644, so sweeping it in would publish a session
#     token — and it is re-minted from credentials we already carry anyway.
w(os.path.join(STATE, "storytel-session.json"),
  json.dumps({"jwt": "SECRET-BEARER", "at": 1}), 0o600)
w(os.path.join(STATE, "backup-last.json"), json.dumps({"ok_at": 1}))
m2 = backup.collect(tempfile.mkdtemp())
paths2 = {e["path"] for e in m2["files"]}
assert os.path.join(STATE, "storytel-session.json") not in paths2, \
    "the cached JWT must not be backed up (it would restore world-readable)"
assert os.path.join(STATE, "backup-last.json") not in paths2, \
    "our own run bookkeeping must not be restored over"
# every file we DO take from the progress tier must be safe to restore 0644
for e in m2["files"]:
    if e["tier"] == "progress":
        assert "session" not in os.path.basename(e["path"]), \
            f"a session credential slipped into the progress tier: {e['path']}"
print("8. the cached JWT and our own bookkeeping stay out of the set OK")

# --- 9. an env-overridden config path is still captured ---------------------
#     The owning modules read these paths from env vars; hardcoding the
#     filenames here would silently back up NOTHING for a relocated file —
#     a backup that looks like it works (architect review 2026-08-17).
assert os.path.join(ETC, "storytel.json") in paths, \
    "the storytel credentials path must come from the same env var " \
    "storytel.py uses, not a hardcoded literal"
import importlib  # noqa: E402
os.environ["VIBB_STORYTEL_CREDS"] = os.path.join(ETC, "moved-storytel.json")
w(os.environ["VIBB_STORYTEL_CREDS"], json.dumps({"email": "x"}), 0o600)
importlib.reload(backup)
moved = {e["path"] for e in backup.collect(tempfile.mkdtemp())["files"]}
assert os.environ["VIBB_STORYTEL_CREDS"] in moved, \
    "a relocated credentials file must still be backed up"
del os.environ["VIBB_STORYTEL_CREDS"]
importlib.reload(backup)
print("9. a config file relocated by env var is still captured OK")

print("\nBACKUP ROUNDTRIP OK — right files, atomic restore, secrets 0600, "
      "a hostile manifest changes nothing, and no session token ships.")
