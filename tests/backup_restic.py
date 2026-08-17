#!/usr/bin/env python3
"""The thin restic/rclone wrappers: env plumbing, arg shape, output parsing.

restic and rclone are faked with tiny scripts that record their calls and
emit canned output — no repo, no Jottacloud. What must hold:
  1. configure_remote writes the repo password 0600, creates the rclone
     remote from the pasted Jottacloud token, and inits the repo when absent;
  2. backup_now collects and parses the snapshot id out of restic's --json;
  3. snapshots() returns newest-first;
  4. restore_snapshot pulls the tree restic produced and applies it live.
"""
import json
import os
import stat
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))

ETC = tempfile.mkdtemp()
STATE = tempfile.mkdtemp()
ART = tempfile.mkdtemp()
GO = tempfile.mkdtemp()
RUN = tempfile.mkdtemp()
BIN = tempfile.mkdtemp()
RCLONE_LOG = os.path.join(BIN, "rclone.log")
RESTIC_LOG = os.path.join(BIN, "restic.log")

os.environ.update({
    "VIBB_ETC": ETC, "VIBB_STATE": STATE, "VIBB_ART": ART,
    "VIBB_GO_CONFIG": os.path.join(GO, "config.yml"), "VIBB_RUN": RUN,
    "VIBB_RESTIC_BIN": os.path.join(BIN, "restic"),
    "VIBB_RCLONE_BIN": os.path.join(BIN, "rclone"),
    "VIBB_RCLONE_CONF": os.path.join(ETC, "rclone.conf"),
    "VIBB_RESTIC_PASS": os.path.join(ETC, "restic-pass"),
    "VIBB_BACKUP_CONF": os.path.join(ETC, "backup.json"),
    "FAKE_RCLONE_LOG": RCLONE_LOG, "FAKE_RESTIC_LOG": RESTIC_LOG,
})

FAKE_RESTIC = r'''#!/usr/bin/env python3
import os, sys, json, shutil
args = sys.argv[1:]
a, i = [], 0
while i < len(args):          # strip global "-o key=val" pairs
    if args[i] == "-o":
        i += 2; continue
    a.append(args[i]); i += 1
if os.environ.get("FAKE_RESTIC_LOG"):
    open(os.environ["FAKE_RESTIC_LOG"], "a").write(" ".join(a) + "\n")
cmd = a[0] if a else ""
if cmd == "cat":        # `cat config` -> repo does not exist yet
    sys.exit(1)
if cmd == "init":
    sys.exit(0)
if cmd == "backup":
    print(json.dumps({"message_type": "status", "percent_done": 1.0}))
    print(json.dumps({"message_type": "summary", "snapshot_id": "deadbeef"}))
    sys.exit(0)
if cmd == "forget":
    sys.exit(0)
if cmd == "snapshots":
    print(json.dumps([
        {"short_id": "1111", "time": "2026-08-16T10:00:00Z", "hostname": "vibb"},
        {"short_id": "2222", "time": "2026-08-17T10:00:00Z", "hostname": "vibb"},
    ]))
    sys.exit(0)
if cmd == "restore":
    target = a[a.index("--target") + 1]
    src = os.environ["FAKE_RESTIC_RESTORE_SRC"]
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(target, name)
        shutil.copytree(s, d) if os.path.isdir(s) else shutil.copy2(s, d)
    sys.exit(0)
sys.exit(0)
'''

FAKE_RCLONE = r'''#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
if os.environ.get("FAKE_RCLONE_LOG"):
    open(os.environ["FAKE_RCLONE_LOG"], "a").write(" ".join(a) + "\n")
sys.exit(0)      # `lsd remote:` -> reachable
'''


def _install(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IRWXU)


_install(os.environ["VIBB_RESTIC_BIN"], FAKE_RESTIC)
_install(os.environ["VIBB_RCLONE_BIN"], FAKE_RCLONE)

from vibb import backup  # noqa: E402


def w(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


w(os.path.join(ETC, "library.json"), json.dumps({"version": 1, "sections": []}))
w(os.path.join(ETC, "storytel.json"), json.dumps({"email": "a@b"}), 0o600)
w(os.path.join(STATE, "a1b2c3.json"), json.dumps({"pos": 1.0, "episodes": {}}))

# --- 1. configure: any pasted rclone remote, no provider baked in -----------
# The owner runs `rclone config` on their own machine (which handles OAuth
# backends too) and pastes the resulting block. Nothing here knows or cares
# which provider it is — this one happens to be S3.
PASTED = "[myremote]\ntype = s3\nprovider = Other\naccess_key_id = AK\n"
assert not backup.configured()
st = backup.configure(PASTED, repo_password="repo-pass-123")
assert backup.configured(), "after setup, configured() must be true"
assert st["repo"] == "rclone:myremote:vibb-backup", \
    f"the repo must be derived from the pasted remote: {st}"
assert (os.stat(os.environ["VIBB_RESTIC_PASS"]).st_mode & 0o777) == 0o600, \
    "the repo password file must be 0600"
assert (os.stat(os.environ["VIBB_RCLONE_CONF"]).st_mode & 0o777) == 0o600, \
    "the pasted rclone config holds backend credentials — it must be 0600"
assert open(os.environ["VIBB_RESTIC_PASS"]).read().strip() == "repo-pass-123"
assert "myremote" in open(os.environ["VIBB_RCLONE_CONF"]).read()
assert "lsd myremote:" in open(RCLONE_LOG).read(), \
    "the remote must be validated with lsd before we trust it"
restic_calls = open(RESTIC_LOG).read()
assert "cat config" in restic_calls and "init" in restic_calls, \
    "an absent repo must be init'd"
print("1. configure accepts any pasted rclone remote, 0600, and inits OK")

# 1b. a repo naming a remote the pasted config does not define is refused —
#     otherwise the box stores a backend it can never reach.
try:
    backup.configure(PASTED, repo="rclone:typo:vibb", repo_password="p")
    assert False, "a repo naming an undeclared remote must be refused"
except RuntimeError as e:
    assert "typo" in str(e)
# a paste with no [section] at all is refused too
try:
    backup.configure("not a config", repo_password="p")
    assert False, "a paste with no remote section must be refused"
except RuntimeError:
    pass
print("1b. a repo pointing at an undeclared remote is refused OK")

# --- 2. backup_now ----------------------------------------------------------
open(RESTIC_LOG, "w").close()
res = backup.backup_now()
assert res["snapshot_id"] == "deadbeef", \
    f"the snapshot id must be parsed from restic --json: {res}"
assert res["files"] >= 2, "the collected file count is reported"
calls = open(RESTIC_LOG).read()
assert "backup --json" in calls, "restic backup must run with --json"
assert "forget --prune" in calls, "retention must run after the backup"
print("2. backup_now snapshots and parses the id, then prunes OK")

# --- 3. snapshots newest-first ----------------------------------------------
snaps = backup.snapshots()
assert [s["id"] for s in snaps] == ["2222", "1111"], \
    f"snapshots must be newest-first: {snaps}"
print("3. snapshots() lists newest-first OK")

# --- 4. restore_snapshot pulls restic's tree and applies it -----------------
# build the tree restic would restore: collect the live box into a fixture,
# then wipe the live files and prove restore brings them back.
fixture = tempfile.mkdtemp()
backup.collect(fixture)
os.environ["FAKE_RESTIC_RESTORE_SRC"] = fixture
lib = os.path.join(ETC, "library.json")
os.remove(lib)
os.remove(os.path.join(STATE, "a1b2c3.json"))

manifest = backup.restore_snapshot("latest")
assert os.path.isfile(lib), "restore must bring the library back"
assert os.path.isfile(os.path.join(STATE, "a1b2c3.json")), \
    "restore must bring the bookmark back"
assert (os.stat(os.path.join(ETC, "storytel.json")).st_mode & 0o777) == 0o600
assert any(e["tier"] == "secret" for e in manifest["files"])
print("4. restore_snapshot pulls the repo tree and applies it live OK")

# --- 5. A FAILED SETUP MUST NOT LEAVE THE BOX LOOKING CONNECTED -------------
#     Committing the repo pointer before the backend answered left a box that
#     rendered "Connected — backing up to ..." in the PWA while every 6h run
#     failed against a repo that was never initialised. The owner believes
#     they have backups; they have none (QA 2026-08-17).
good = backup.load_repo()
os.environ["FAKE_RCLONE_FAIL"] = "1"
FAKE_RCLONE_FAILING = FAKE_RCLONE.replace(
    "sys.exit(0)      # `lsd remote:` -> reachable",
    "sys.exit(1)      # unreachable backend")
_install(os.environ["VIBB_RCLONE_BIN"], FAKE_RCLONE_FAILING)
try:
    backup.configure("[broken]\ntype = s3\n", repo_password="p2")
    assert False, "an unreachable backend must fail setup"
except RuntimeError as e:
    assert "reach" in str(e).lower(), e
assert backup.load_repo() == good, \
    "a failed setup must not repoint the box at the repo it could not reach"
assert open(os.environ["VIBB_RESTIC_PASS"]).read().strip() == "repo-pass-123", \
    "a failed setup must roll the password back, not leave the new one"
assert "broken" not in open(os.environ["VIBB_RCLONE_CONF"]).read(), \
    "a failed setup must roll rclone.conf back"
_install(os.environ["VIBB_RCLONE_BIN"], FAKE_RCLONE)   # heal for later runs
print("5. a failed setup rolls back and leaves the old backend intact OK")

# --- 6. status() reports whether backups are actually WORKING ---------------
#     'configured' is not health: a box whose every run failed for a month
#     looks identical to a healthy one without a last-success time.
st = backup.status()
assert st["last_ok"], \
    "a successful backup must record when it last succeeded"
print("6. status() carries the last successful backup time OK")

# --- 7. the timer entry point is a no-op when the clock is not trusted ------
#     The Zero has no RTC: early in a boot the wall clock is roughly 'when the
#     box was last switched off', and restic buckets retention by CALENDAR
#     DAY. Backing up under a wrong clock files snapshots in the wrong bucket.
#     The rest of the codebase waits for clock_trusted(); so does this.
open(RESTIC_LOG, "w").close()
backup.clock_trusted = lambda: False
assert backup.main() == 0, "an untrusted clock is a clean no-op, not a failure"
assert "backup" not in open(RESTIC_LOG).read(), \
    "no snapshot may be taken while the clock is untrusted"
backup.clock_trusted = lambda: True
assert backup.main() == 0
assert "backup --json" in open(RESTIC_LOG).read(), \
    "with a trusted clock the timer entry point does back up"
print("7. the timer skips cleanly until the clock is trusted OK")

print("\nBACKUP RESTIC OK — setup rolls back on failure, health is visible, "
      "and the timer waits for a trustworthy clock.")
