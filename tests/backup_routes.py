#!/usr/bin/env python3
"""The backup daemon endpoints: every one of them privileged.

This is the security-critical half. The backup set carries the Storytel
password, the Spotify credentials and every child's listening position, and
/backup/restore WRITES all of that onto the live box. So:

  - the POSTs are privileged for free, by default-deny (SAFE lists none);
  - the two GETs are NOT, because SAFE["GET"] is True for the whole LAN —
    they must gate themselves, and this pins that they do. /backup/status
    names the owner's bucket and /backup/snapshots is a history of the box;
    neither is booleans-only, so neither may be open the way
    /storytel/status is.

No restic binary: the module's own wrappers are faked, so this tests the
routing and the gate, not the engine (backup_restic.py covers that).
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "api-token")
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
os.environ["VIBB_ETC"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import backup, token  # noqa: E402

TOKEN = token.ensure()

# --- fake the engine, keep the routing ---------------------------------------
CALLS = []
REBOOTS = []
backup.status = lambda: {"configured": True,
                         "repo": "rclone:myremote:vibb-backup"}
backup.snapshots = lambda: [{"id": "2222", "time": "2026-08-17T10:00:00Z",
                             "hostname": "vibb"}]
backup.backup_now = lambda: (CALLS.append("backup_now"),
                             {"snapshot_id": "deadbeef", "created": 1,
                              "files": 7})[1]
backup.restore_snapshot = lambda snap="latest": (
    CALLS.append(("restore", snap)),
    {"files": [{"path": "/etc/vibb/library.json"}], "created": 1})[1]


def _configure(conf, repo=None, repo_password=None, path=None):
    if not conf:
        raise RuntimeError("paste an rclone remote configuration first")
    CALLS.append(("configure", conf, repo, path))
    return {"configured": True, "repo": repo or "rclone:myremote:vibb-backup"}


backup.configure = _configure
daemon.subprocess.run = lambda *a, **k: REBOOTS.append(a) or None

srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, method="POST", body=None, tok=None):
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("X-Vibb-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# --- 1. the POSTs are privileged by default-deny -----------------------------
for path, b in (("/backup/configure", {"rclone_conf": "[r]\ntype = s3\n",
                                       "repo_password": "p"}),
                ("/backup/now", {}),
                ("/backup/restore", {})):
    st, _ = call(path, body=b)
    assert st == 401, f"{path} must be privileged, got {st}"
assert not CALLS, "a refused call must not have reached the engine"
assert not REBOOTS, "a refused restore must never reboot the box"
print("1. every backup POST is privileged (default-deny) OK")

# --- 2. the GETs gate themselves despite SAFE["GET"] being True --------------
for path in ("/backup/status", "/backup/snapshots"):
    st, body = call(path, method="GET")
    assert st == 401, \
        (f"{path} must gate itself — SAFE['GET'] is True, so an ungated "
         f"handler is open to the whole LAN (got {st})")
    assert "myremote" not in body, "a denial must not leak the backend"
print("2. both backup GETs gate themselves against the open-GET default OK")

# --- 3. with the token they work --------------------------------------------
st, body = call("/backup/status", method="GET", tok=TOKEN)
assert st == 200 and json.loads(body)["configured"] is True, (st, body)
st, body = call("/backup/snapshots", method="GET", tok=TOKEN)
assert st == 200 and json.loads(body)["snapshots"][0]["id"] == "2222", body
print("3. status and snapshots answer with the token OK")

# --- 4. configure passes the pasted block through, and validates ------------
st, body = call("/backup/configure", tok=TOKEN,
                body={"rclone_conf": "[myremote]\ntype = s3\n",
                      "repo_password": "p", "path": "vibb-backup"})
assert st == 200, (st, body)
assert any(c[0] == "configure" for c in CALLS if isinstance(c, tuple)), CALLS
# an empty paste is a 400 from the engine's own validation, not a 500
st, body = call("/backup/configure", tok=TOKEN,
                body={"rclone_conf": "", "repo_password": "p"})
assert st == 400, f"an empty config must be a clean 400: {st} {body}"
print("4. configure accepts a pasted remote and rejects an empty one OK")

# --- 5. backup runs; restore applies AND reboots ----------------------------
st, body = call("/backup/now", tok=TOKEN)
assert st == 200 and json.loads(body)["snapshot_id"] == "deadbeef", body
st, body = call("/backup/restore", tok=TOKEN, body={"snapshot": "2222"})
assert st == 200, (st, body)
data = json.loads(body)
assert data["restored"] == 1 and data["rebooting"] is True, data
assert ("restore", "2222") in CALLS, "the chosen snapshot must be passed through"
# the reboot is fired on a thread after the response — give it a moment
for _ in range(50):
    if REBOOTS:
        break
    import time as _t
    _t.sleep(0.05)
assert any("reboot" in " ".join(a[0]) for a in REBOOTS), \
    "a restore must reboot: the daemon would otherwise write its in-memory " \
    "library back over the restored one"
print("5. backup_now snapshots, and restore applies then reboots OK")

print("\nBACKUP ROUTES OK — all privileged, the open-GET default is "
      "overridden, and a restore reboots.")
