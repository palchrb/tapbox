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
# set the other gates aside so this pins the CLOCK one alone
backup.DAILY_GATE = False
_real_box_busy = backup._box_busy   # tests below stub it; 17 restores
backup._box_busy = lambda: False
backup.clock_trusted = lambda: False
assert backup.main() == 0, "an untrusted clock is a clean no-op, not a failure"
assert "backup" not in open(RESTIC_LOG).read(), \
    "no snapshot may be taken while the clock is untrusted"
backup.clock_trusted = lambda: True
assert backup.main() == 0
assert "backup --json" in open(RESTIC_LOG).read(), \
    "with a trusted clock the timer entry point does back up"
print("7. the timer skips cleanly until the clock is trusted OK")

# --- 8. THE BACKUP STANDS DOWN WHILE THE BOX IS BUSY ------------------------
#     An upload shares the single 2.4GHz radio with the Spotify stream AND
#     the A2DP link; a TLS burst mid-playback is the documented stutter and
#     firmware-crash trigger on this hardware. A backup is always the side
#     that can wait. Crucially it must DEFER, never quiesce: stopping a
#     child's audiobook to upload 1MB of JSON is the worst available trade.
backup.DAILY_GATE = False        # don't let the daily gate mask this
backup.BUSY_WAIT_S = 0           # give up immediately rather than sleep
open(RESTIC_LOG, "w").close()
backup._box_busy = lambda: True
assert backup.main() == 0, "a busy box is a clean no-op, not a failure"
assert "backup" not in open(RESTIC_LOG).read(), \
    "no snapshot may be taken while the box is playing or BT is live"
backup._box_busy = lambda: False
assert backup.main() == 0
assert "backup --json" in open(RESTIC_LOG).read(), \
    "an idle box does back up"
print("8. the backup defers while playing/BT is live, and runs when idle OK")

# 9. ...and the busy check FAILS OPEN — a broken probe must never stall
#    backups forever (the rule library.py's own busy check follows).
import urllib.request as _u  # noqa: E402


def _boom(*a, **k):
    raise OSError("daemon not answering")


_real_urlopen, _u.urlopen = _u.urlopen, _boom
assert backup._box_busy() is False, \
    "an unreachable daemon must read as 'not busy', never block backups"
_u.urlopen = _real_urlopen
print("9. an unreachable daemon fails open OK")

# --- 10. the daily gate is wall-clock, so a reboot cannot reset it ----------
#     A monotonic systemd timer restarts at every boot; on a toddler-power-
#     cycled box that meant one backup per boot, 15 min into a listening
#     session. The cadence lives here instead.
backup.DAILY_GATE = True
open(RESTIC_LOG, "w").close()
assert backup.main() == 0
assert "backup --json" not in open(RESTIC_LOG).read(), \
    "a backup taken minutes ago must not be repeated on the next wake"
print("10. the daily cadence is wall-clock and survives reboots OK")

# --- 11. a timed-out restic still reports the failure ----------------------
#     TimeoutExpired is not a RuntimeError; catching only RuntimeError meant
#     a hung run left last_error stale, so a box failing every run looked
#     healthy — defeating the field the check exists for.
backup.DAILY_GATE = False
_real_backup_now = backup.backup_now
backup.backup_now = lambda **kw: (_ for _ in ()).throw(
    __import__("subprocess").TimeoutExpired("restic", 300))
assert backup.main() == 1, "a timed-out backup must report failure"
assert backup.status()["last_error"], \
    "a timeout must reach last_error, not just the journal"
print("11. a timed-out run is recorded, not silently lost OK")

# --- 12. A RUNNING BACKUP STANDS DOWN WHEN THE MUSIC STARTS ----------------
#     Checking once before we start is not enough: a run takes tens of
#     seconds and a kid can tap play at any point inside it. The content
#     sweeper already terminates its child mid-download for exactly this
#     reason. An abandoned backup costs nothing — restic dedups, so the
#     next run re-uploads only what is still missing.
backup.backup_now = _real_backup_now      # undo test 11's stub
SLOW_RESTIC = FAKE_RESTIC.replace(
    'if cmd == "backup":',
    'if cmd == "backup":\n    import time as _t; _t.sleep(3)')
_install(os.environ["VIBB_RESTIC_BIN"], SLOW_RESTIC)
backup.WATCH_POLL_S = 0.1
backup.DAILY_GATE = False
started = [0]


def _busy_after_first_look():
    started[0] += 1
    return started[0] > 1       # idle when we decide to start, busy once in


backup._box_busy = _busy_after_first_look
err_before = backup.status()["last_error"]
rc = backup.main()
assert rc == 0, "standing down is not a failure — nothing is broken"
assert backup.status()["last_error"] == err_before, \
    "a yield must not be recorded as an error; nothing is broken"
print("12. a backup already running stands down when playback starts OK")

# 13. ...and a yield does NOT count as a successful backup, so the daily
#     gate still lets the next shutdown try again.
before = backup.status()["last_ok"]
backup._box_busy = _busy_after_first_look
started[0] = 0
backup.main()
assert backup.status()["last_ok"] == before, \
    "a yielded run must not stamp a success, or it would suppress retries"
_install(os.environ["VIBB_RESTIC_BIN"], FAKE_RESTIC)   # heal
print("13. a yielded run does not claim success OK")

print("\nBACKUP RESTIC OK — setup rolls back, health is visible, the timer "
      "waits for a trustworthy clock, and the music always wins the radio.")

# --- 14. the restraints travel with the CALL, not the unit file -------------
#     The PWA's routes call this module in-process from vibb-daemon, whose
#     unit carries no limits — so anything set only in vibb-backup.service
#     protected the timer path and left the daemon path running with Go's
#     defaults and no restic cache, next to mpv (QA 2026-08-17).
env = backup._restic_env()
assert env["GOMEMLIMIT"] == "80MiB" and env["GOGC"] == "20", env
assert env["GOMAXPROCS"] == "1", "one core, like the content sweeper's taskset"
assert env["RESTIC_CACHE_DIR"], \
    "without a cache dir restic re-fetches the whole index every run"
os.environ["GOMEMLIMIT"] = "40MiB"     # a unit file may still tighten them
assert backup._restic_env()["GOMEMLIMIT"] == "40MiB"
del os.environ["GOMEMLIMIT"]
print("14. memory/CPU restraints travel with the call, not the unit OK")

# --- 15. THE CADENCE IS A CALENDAR DAY, NOT 24 ELAPSED HOURS ---------------
#     Elapsed-time gating drifted: back up at 16:40 Monday, and Tuesday's
#     16:10 shutdown is only 23.5h later -> skipped, and the bookmarks wait
#     for Wednesday. Every day that ends earlier than the day before is lost,
#     which for a child who listens each afternoon is every-other-day in
#     practice (owner 2026-08-19). Calendar day also matches restic's own
#     retention bucketing.
import time as _time  # noqa: E402

_now = _time.time()
assert backup._backed_up_today(_now), "a backup just taken IS today's"
assert backup._backed_up_today(_now - 8 * 3600) or \
    _time.localtime(_now).tm_hour < 8, \
    "earlier the same day still counts as done today"
# 23.5h ago is yesterday — UNLESS the clock is past 23:30, when it lands
# back on today. The mirror of the tm_hour < 8 guard above: without it this
# pin fails on healthy code for 30 minutes every night (cloud review
# 2026-08-20). A fixed timestamp can't replace the guard — _backed_up_today
# compares against time.localtime() itself, so "today" always moves.
_lt = _time.localtime(_now)
_sod = _lt.tm_hour * 3600 + _lt.tm_min * 60 + _lt.tm_sec
assert not backup._backed_up_today(_now - int(23.5 * 3600)) \
    or _sod >= int(23.5 * 3600), \
    ("23.5h ago is a different calendar day — this is the drift bug: an "
     "elapsed-hours gate skips today's backup entirely")
assert not backup._backed_up_today(_now - 24 * 3600), "yesterday is not today"
print("15. the daily gate is a calendar day, so it cannot drift OK")

# ---- 16. `python3 -m vibb.backup` — the path systemd actually runs ---------
# The timer and the idle-shutdown hook both execute the MODULE, top to
# bottom. From 17ae067 to 2026-08-20 the __main__ guard sat mid-file, so
# main() ran before the defs below it existed and every unit run that got
# past the gates died with NameError: '_mkstaging' — while imports (the
# daemon, the PWA's Back-up-now, every test in this file) loaded the whole
# file and worked. Field journal 2026-08-20. Only a real -m subprocess can
# pin this class of bug.
import subprocess  # noqa: E402

_env = dict(os.environ)
_blank = tempfile.mkdtemp()                    # unconfigured backend: the
_env["VIBB_ETC"] = _blank                      # conf var must move too, or
_env["VIBB_BACKUP_CONF"] = os.path.join(_blank, "backup.json")  # the setup
_env["PYTHONPATH"] = os.path.join(REPO, "pi")  # from test 1 leaks in
_r = subprocess.run([sys.executable, "-m", "vibb.backup"],
                    capture_output=True, text=True, env=_env, timeout=30)
assert "no backend configured" in _r.stdout, (_r.stdout, _r.stderr)
assert "NameError" not in _r.stderr, _r.stderr
assert _r.returncode == 0, (_r.returncode, _r.stderr)
# and the guard itself must stay LAST, or the -m path silently loses
# whatever gets appended below it next time
_src = open(backup.__file__, encoding="utf-8").read()
assert _src.rstrip().endswith("sys.exit(main())"), \
    "the __main__ guard must be the last statement in vibb/backup.py"
print("16. python3 -m vibb.backup runs whole (the systemd path) OK")

# ---- 17. on the way down, a silent BT link no longer counts as busy --------
# At idle shutdown a connected-but-silent speaker kept _box_busy() true
# (bt_connected = live A2DP LINK, not live audio), so the shutdown backup
# died waiting, killed by vibb-idle's own timeout (field journal
# 2026-08-20: 190s then TERM, every time). vibb-idle now stamps
# RUN_DIR/poweroff-imminent first; while the marker is fresh, bt_connected
# alone stops counting. The exception lives in _box_busy itself — NOT the
# wait loop — because backup_now(watch=True) polls the same predicate
# mid-run; a loop-only bypass just moved the stall three seconds later.
# The owner's invariant holds: `playing` still aborts (test 12 pins it),
# so nothing wifi-heavy ever overlaps ACTIVE A2DP playback.
backup.DAILY_GATE = False
backup.BUSY_WAIT_S = 0
backup._box_busy = _real_box_busy      # undo test 12/13's stub
_real_urlopen = backup.urllib.request.urlopen


class _FakeStatus:                       # speaker on, nothing playing
    def read(self):
        return json.dumps({"playing": False, "bt_connected": True}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


backup.urllib.request.urlopen = lambda url, timeout=5: _FakeStatus()
_marker = os.path.join(RUN, "poweroff-imminent")

# no marker (the TIMER path): the silent link still defers, as always
open(RESTIC_LOG, "w").close()
assert backup.main() == 0
assert "backup" not in open(RESTIC_LOG).read(), \
    "the timer path must keep deferring to a connected speaker"

# fresh marker (the idle-shutdown path): the run completes
with open(_marker, "w") as f:
    f.write("now")
open(RESTIC_LOG, "w").close()
assert backup.main() == 0
assert "backup --json" in open(RESTIC_LOG).read(), \
    "the shutdown backup must run despite a silent connected speaker"

# a STALE marker (leftover, clock weirdness) keeps the old behaviour
_old = os.path.getmtime(_marker)
os.utime(_marker, (_old - 3600, _old - 3600))
open(RESTIC_LOG, "w").close()
assert backup.main() == 0
assert "backup" not in open(RESTIC_LOG).read(), \
    "an old marker must not bypass the busy check"

os.remove(_marker)
backup.urllib.request.urlopen = _real_urlopen
print("17. poweroff-imminent discounts the silent link; stale does not OK")

# ...and vibb-idle actually stamps it before starting the unit
_idle_src = open(os.path.join(REPO, "pi", "idle.py"), encoding="utf-8").read()
_i_mark = _idle_src.index('"poweroff-imminent"')
_i_start = _idle_src.index('"systemctl", "start"')
assert _i_mark < _i_start, \
    "idle.py must stamp poweroff-imminent BEFORE starting vibb-backup"
print("17b. idle.py stamps the marker before the unit starts OK")
