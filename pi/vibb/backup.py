"""Backup & restore of the box's critical state to a restic repo.

vibb keeps everything as small JSON/plaintext files on the SD card. When the
card dies (and a battery box yanked by toddlers kills cards), the owner loses
the whole curated library, all settings, the token secret, the Storytel and
Spotify logins, RFID mappings, the BT speaker MAC, and every child's place in
every book. None of that is downloadable again.

This module snapshots exactly that critical state — CONFIG + SECRET +
PROGRESS, never the regenerable caches or the GBs of downloaded audio — into
a **restic** repo. restic is the engine because it brings authenticated
encryption, dedup, incremental snapshots, retention and an integrity check
for free — less crypto we own, and it fails *cleanly* on a wrong password
rather than an unauthenticated cipher decrypting to garbage.

The backend is the owner's choice, not ours: restic reaches it through
**rclone**, so any of rclone's ~70 backends (S3, B2, WebDAV/Nextcloud,
Jottacloud, Drive, Dropbox, SFTP …) works. The owner sets it up entirely in
the PWA by pasting an rclone remote config block; nothing here is tied to any
one provider. The restic repo is then `rclone:<remote>:<path>`.

Kept deliberately self-contained — imports only `vibb.paths` — so it never
reintroduces the wide-import coupling that has bitten the subprocess modules.
The restic/rclone shell-outs live behind thin wrappers; the load-bearing
logic (which files, and the atomic two-phase restore) needs no binary and is
unit-tested on a tmp tree.
"""

import glob
import json
import os
import shutil
import subprocess
import time

from vibb.paths import ART_DIR, STATE_DIR

# --- locations (env-overridable so tests point them at a tmp tree) ----------
ETC = os.environ.get("VIBB_ETC", "/etc/vibb")
RESTIC_BIN = os.environ.get("VIBB_RESTIC_BIN", "restic")
RCLONE_BIN = os.environ.get("VIBB_RCLONE_BIN", "rclone")
RCLONE_CONF = os.environ.get("VIBB_RCLONE_CONF", os.path.join(ETC, "rclone.conf"))
RESTIC_PASS_FILE = os.environ.get(
    "VIBB_RESTIC_PASS", os.path.join(ETC, "restic-pass"))
# Where the chosen restic repo string ({"repo": "rclone:remote:path"}) lives.
# Written at setup, read on every call — the backend is not known at import.
BACKUP_CONF = os.environ.get("VIBB_BACKUP_CONF", os.path.join(ETC, "backup.json"))
DEFAULT_REPO_PATH = "vibb-backup"

# go-librespot's config dir holds credentials.json + state.json. The daemon
# already knows it via VIBB_GO_CONFIG (a path to config.yml); we take its
# directory. Empty when unset -> those two files are simply skipped.
_GO_CONFIG = os.environ.get("VIBB_GO_CONFIG", "")
GO_DIR = os.path.dirname(_GO_CONFIG) if _GO_CONFIG else ""

MANIFEST_FORMAT = "vibb-backup/1"
# Bump when a restore would need code this version lacks. A backup whose
# schema is HIGHER than this is refused (it may carry fields we cannot apply);
# an equal-or-lower one is fine — each module already migrates old shapes on
# read (bookmarks.py, library.py).
SCHEMA = 1

# Files under STATE_DIR that are transient flow state, NOT progress worth
# keeping — rewritten every play/poll. Everything else ending in .json is
# taken (arbitrary bookmark keys included); non-.json markers like
# bt-connect-kick / bt-quiet are skipped by the .json filter.
_STATE_EXCLUDE = frozenset({
    "now-playing.json", "now-queue.json", "output.json", "renderer.json",
    "sonos.json", "last-sweep.json", "spotify-precache.json",
    "podcast-new.json", "on-battery-runtime.json",
    "storytel-shelf-raw.json", "storytel-login-refused.json",
})

RESTORE_TMP_SUFFIX = ".vibbrestore.tmp"


# --- the whitelist ----------------------------------------------------------
def _config_files():
    out = []
    for name in ("library.json", "settings.json", "cards.json",
                 "rfid.conf", "bt-headset"):
        p = os.path.join(ETC, name)
        if os.path.exists(p):
            out.append(p)
    out += sorted(glob.glob(os.path.join(ART_DIR, "section-*.jpg")))
    return out


def _secret_files():
    out = []
    for name in ("storytel.json", "spotify-api.json"):
        p = os.path.join(ETC, name)
        if os.path.exists(p):
            out.append(p)
    if GO_DIR:
        for name in ("credentials.json", "state.json"):
            p = os.path.join(GO_DIR, name)
            if os.path.exists(p):
                out.append(p)
    return out


def _progress_files():
    out = []
    try:
        names = sorted(os.listdir(STATE_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name in _STATE_EXCLUDE:
            continue
        out.append(os.path.join(STATE_DIR, name))
    return out


def _iter_files():
    for p in _config_files():
        yield p, "config"
    for p in _secret_files():
        yield p, "secret"
    for p in _progress_files():
        yield p, "progress"


def _box_id():
    try:
        with open("/etc/machine-id") as f:
            return f.read().strip()
    except OSError:
        return ""


# --- collect: build the file set + manifest in a staging dir ----------------
def collect(staging):
    """Copy the whitelist into <staging>/files/<abspath>, write manifest.json.
    Returns the manifest dict. No restic, no network — this is the unit that
    decides WHAT is backed up, and is tested directly."""
    files_root = os.path.join(staging, "files")
    entries = []
    for src, tier in _iter_files():
        try:
            st = os.stat(src)
        except OSError:
            continue
        dst = os.path.join(files_root, src.lstrip("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        entries.append({"path": src, "tier": tier,
                        "mode": oct(st.st_mode & 0o777)})
    manifest = {
        "format": MANIFEST_FORMAT,
        "schema": SCHEMA,
        "created": int(time.time()),
        "box_id": _box_id(),
        "tiers": sorted({e["tier"] for e in entries}),
        "token_included": False,   # api-token is deliberately never in the set
        "files": entries,
    }
    with open(os.path.join(staging, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


# --- restore: two-phase atomic apply of an already-restored tree ------------
def _check_restorable(manifest):
    fmt = manifest.get("format")
    if fmt != MANIFEST_FORMAT:
        raise ValueError(f"unknown backup format {fmt!r}")
    if int(manifest.get("schema") or 0) > SCHEMA:
        raise ValueError(
            f"backup schema {manifest.get('schema')} is newer than this box "
            f"understands ({SCHEMA}) — upgrade vibb before restoring")


def _target_owner(dest):
    """(uid, gid) the restored file should end up owned by: the existing
    file's owner, else the deepest existing ancestor dir's owner (so a
    go-librespot cred restored before its dir exists still lands owned by the
    run user who owns ~/.config, not root). None when nothing exists yet."""
    if os.path.lexists(dest):
        st = os.lstat(dest)
        return (st.st_uid, st.st_gid)
    d = os.path.dirname(dest)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    if d:
        st = os.stat(d)
        return (st.st_uid, st.st_gid)
    return None


def _makedirs_owned(path, owner):
    """makedirs, chowning any directories we create to `owner` so a fresh
    ~/.config/go-librespot the run user must read is not left root-owned."""
    if os.path.isdir(path):
        return
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        _makedirs_owned(parent, owner)
    os.mkdir(path)
    if owner:
        try:
            os.chown(path, *owner)
        except OSError:
            pass


def apply_tree(tree):
    """Apply a restored tree (<tree>/manifest.json + <tree>/files/...) to the
    live filesystem, atomically. Two phases: validate + stage every file,
    then a rename sweep — so a bundle that fails validation writes nothing,
    and a crash mid-commit still leaves each individual file whole (old or
    new, never torn). Reused by restore_snapshot; tested directly."""
    with open(os.path.join(tree, "manifest.json")) as f:
        manifest = json.load(f)
    _check_restorable(manifest)
    files_root = os.path.join(tree, "files")

    # Phase 1 — validate and stage beside each destination.
    staged = []   # (tmp, dest, owner)
    try:
        for entry in manifest["files"]:
            dest = entry["path"]
            src = os.path.join(files_root, dest.lstrip("/"))
            if not os.path.isfile(src):
                raise ValueError(f"backup is missing its file for {dest}")
            with open(src, "rb") as f:
                data = f.read()
            if dest.endswith(".json"):
                json.loads(data)   # torn/garbage json -> reject before commit
            # secrets are forced 0600 at creation, never chmod-after; other
            # files keep their recorded mode (default 0644).
            mode = 0o600 if entry.get("tier") == "secret" \
                else int(entry.get("mode", "0o644"), 8)
            owner = _target_owner(dest)   # BEFORE makedirs, so it is the real
            #                               pre-existing owner, not root
            parent = os.path.dirname(dest)
            if parent:
                _makedirs_owned(parent, owner)
            tmp = dest + RESTORE_TMP_SUFFIX
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            staged.append((tmp, dest, owner))
    except BaseException:
        for tmp, _dest, _owner in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise

    # Phase 2 — commit. os.replace is atomic on the same filesystem, which is
    # why the tmp sits beside its destination and not in a shared staging dir.
    for tmp, dest, owner in staged:
        if owner:
            try:
                os.chown(tmp, *owner)
            except OSError:
                pass
        os.replace(tmp, dest)
    return manifest


# --- backend config (generic: any rclone remote, pasted in the PWA) ---------
def load_repo():
    """The configured restic repo string, or "" when unset."""
    try:
        with open(BACKUP_CONF) as f:
            return json.load(f).get("repo") or ""
    except (OSError, ValueError):
        return ""


def configured():
    """True once a backend has been set up (repo pointer + repo password)."""
    return bool(load_repo()) and os.path.exists(RESTIC_PASS_FILE)


def status():
    """Non-secret view for the PWA: is a backend set up, and where to (the
    repo string names the remote, never a credential)."""
    return {"configured": configured(), "repo": load_repo()}


def _remote_name(repo):
    """The rclone remote inside an `rclone:remote:path` repo, else "" (a
    restic-native repo string needs no rclone validation)."""
    if repo.startswith("rclone:"):
        parts = repo.split(":", 2)   # ["rclone", "remote", "path…"]
        if len(parts) >= 2:
            return parts[1]
    return ""


def _rclone_remotes(conf_text):
    """Remote names declared in a pasted rclone.conf ([name] section heads)."""
    names = []
    for line in conf_text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            names.append(line[1:-1].strip())
    return names


def configure(rclone_conf_text, repo=None, repo_password=None, path=None):
    """Set up any rclone-backed restic repo, from values pasted in the PWA.

    `rclone_conf_text` is one or more rclone remote blocks the owner generated
    on their own machine (`rclone config` — which handles OAuth backends too).
    `repo` may be given explicitly (`rclone:remote:path`); otherwise it is
    built from the first pasted remote and `path` (default 'vibb-backup').
    `repo_password` encrypts the restic repo and is stored 0600 on the box.

    Idempotent. Raises RuntimeError on any failure, having stored nothing that
    would leave a half-configured box.
    """
    if not rclone_conf_text or not rclone_conf_text.strip():
        raise RuntimeError("paste an rclone remote configuration first")
    if not repo_password:
        raise RuntimeError("a repo password is required")
    remotes = _rclone_remotes(rclone_conf_text)
    if not repo:
        if not remotes:
            raise RuntimeError("no [remote] section found in the pasted config")
        repo = f"rclone:{remotes[0]}:{path or DEFAULT_REPO_PATH}"
    remote = _remote_name(repo)
    if remote and remote not in remotes:
        raise RuntimeError(
            f"repo names remote {remote!r} but the pasted config has "
            f"{remotes or 'no remotes'}")

    _write_secret_text(RCLONE_CONF, rclone_conf_text)
    _write_secret_json(BACKUP_CONF, {"repo": repo})
    _write_secret_text(RESTIC_PASS_FILE, repo_password)

    env = dict(os.environ)
    env["RCLONE_CONFIG"] = RCLONE_CONF
    if remote:
        try:
            subprocess.run(
                [RCLONE_BIN, "lsd", f"{remote}:"],
                env=env, capture_output=True, text=True, timeout=60,
                check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                OSError) as e:
            detail = (getattr(e, "stderr", "") or str(e)).strip()
            raise RuntimeError(f"could not reach the backend: {detail}")
    # init the repo unless it already answers to this password
    if _restic("cat", "config", timeout=60).returncode != 0:
        r = _restic("init", timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"restic init failed: {r.stderr.strip()}")
    return status()


# --- restic / rclone shell-outs ---------------------------------------------
def _restic_env():
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = load_repo()
    env["RESTIC_PASSWORD_FILE"] = RESTIC_PASS_FILE
    env["RCLONE_CONFIG"] = RCLONE_CONF
    return env


def _restic(*args, timeout=300, check=False):
    """Run restic, pointing it at our rclone (so it need not be on PATH) and
    our repo/password via env. Returns the CompletedProcess."""
    return subprocess.run(
        [RESTIC_BIN, "-o", f"rclone.program={RCLONE_BIN}", *args],
        env=_restic_env(), capture_output=True, text=True,
        timeout=timeout, check=check)


def _write_secret_text(path, text):
    """0600 from creation, atomic rename — the token._write pattern, for raw
    text (rclone.conf, the restic repo password)."""
    _write_secret(path, (text.rstrip("\n") + "\n").encode())


def _write_secret_json(path, obj):
    _write_secret(path, json.dumps(obj).encode())


def _write_secret(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def backup_now(keep=("--keep-daily", "7", "--keep-weekly", "4",
                     "--keep-monthly", "6")):
    """Snapshot the whitelist to the repo, then prune to the retention
    policy. Builds the set in a private tmpfs dir and removes it after.
    Returns {snapshot_id, created, files}. Raises RuntimeError on failure."""
    if not configured():
        raise RuntimeError("no backup backend configured")
    staging = _mkstaging()
    try:
        manifest = collect(staging)
        r = _restic("backup", "--json", os.path.join(staging, "files"),
                    os.path.join(staging, "manifest.json"))
        if r.returncode != 0:
            raise RuntimeError(f"restic backup failed: {r.stderr.strip()}")
        snap_id = _parse_backup_snapshot(r.stdout)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    # retention — best effort; a prune failure must not fail the backup that
    # already succeeded.
    _restic("forget", "--prune", *keep, timeout=600)
    return {"snapshot_id": snap_id, "created": manifest["created"],
            "files": len(manifest["files"])}


def snapshots():
    """List repo snapshots (newest first): [{id, time, hostname}]. Raises
    RuntimeError if the repo cannot be read."""
    if not configured():
        raise RuntimeError("no backup backend configured")
    r = _restic("snapshots", "--json", timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"restic snapshots failed: {r.stderr.strip()}")
    try:
        raw = json.loads(r.stdout or "[]")
    except ValueError:
        raise RuntimeError("restic returned unreadable snapshot list")
    out = [{"id": s.get("short_id") or s.get("id"),
            "time": s.get("time"), "hostname": s.get("hostname")}
           for s in raw]
    out.reverse()   # restic lists oldest-first; the picker wants newest-first
    return out


def restore_snapshot(snapshot="latest"):
    """Pull one snapshot from the repo and apply it atomically to the live
    box. Returns the applied manifest. Raises RuntimeError/ValueError."""
    if not configured():
        raise RuntimeError("no backup backend configured")
    staging = _mkstaging()
    try:
        r = _restic("restore", snapshot, "--target", staging, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"restic restore failed: {r.stderr.strip()}")
        # restic restores the absolute paths we backed up: our staging/files
        # and staging/manifest.json reappear under <staging>.
        return apply_tree(staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _mkstaging():
    """A private 0700 staging dir on tmpfs (RUN_DIR) — never CACHE_DIR, which
    is served, pruned and pushed."""
    import tempfile
    from vibb.paths import RUN_DIR
    base = os.path.join(RUN_DIR, "vibb-backup")
    os.makedirs(base, exist_ok=True)
    os.chmod(base, 0o700)
    return tempfile.mkdtemp(dir=base)


def _parse_backup_snapshot(stdout):
    """restic backup --json emits one JSON object per line; the final
    'summary' message carries snapshot_id."""
    snap = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("message_type") == "summary" and msg.get("snapshot_id"):
            snap = msg["snapshot_id"]
    return snap
