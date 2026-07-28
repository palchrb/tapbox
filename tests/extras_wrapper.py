#!/usr/bin/env python3
"""Gate the tapbox-extra wrapper (pi/extra.sh).

--run must free the hardware BEFORE the script runs (ui/idle/buttons +
go-librespot stopped, playback stopped via the SAFE /stop endpoint) and
exec the script. --restore must unmask BEFORE starting (a script that
masked units would otherwise brick the box) and start the FULL set —
including units the wrapper never stopped, so a script that stopped
tapbox-daemon itself still returns to a whole box. tapbox-daemon must
NOT be stopped by --run (it is the remote escape hatch)."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRAPPER = os.path.join(REPO, "pi", "extra.sh")
TMP = tempfile.mkdtemp()
LOG = os.path.join(TMP, "calls.log")

# fake systemctl: append verb+args to the log
FAKE = os.path.join(TMP, "systemctl")
with open(FAKE, "w") as f:
    f.write(f'#!/bin/sh\necho "$@" >> {LOG}\n')
os.chmod(FAKE, 0o755)

# fake daemon capturing the playback-stop call
STOPS = []


class Daemon(BaseHTTPRequestHandler):
    def do_POST(self):
        STOPS.append((self.path, self.headers.get("Content-Type")))
        out = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Daemon)
threading.Thread(target=srv.serve_forever, daemon=True).start()

env = dict(os.environ, TAPBOX_SYSTEMCTL=FAKE,
           TAPBOX_DAEMON=f"http://127.0.0.1:{srv.server_port}")

# the "extra": records that it ran and inspects what was stopped first
MARK = os.path.join(TMP, "ran")
script = os.path.join(TMP, "game.sh")
with open(script, "w") as f:
    f.write(f'#!/bin/sh\ncp {LOG} {MARK}\n')
os.chmod(script, 0o755)

# 1. --run: playback stopped, hardware owners stopped, THEN the script
r = subprocess.run(["bash", WRAPPER, "--run", script], env=env,
                   capture_output=True, text=True, timeout=30)
assert r.returncode == 0, r.stderr
assert STOPS and STOPS[0] == ("/stop", "application/json"), STOPS
with open(MARK) as f:
    before_script = f.read()
assert "stop tapbox-idle tapbox-buttons tapbox-ui" in before_script
assert "stop go-librespot" in before_script, "the ALSA holder must stop"
assert "tapbox-daemon" not in before_script, \
    "--run must leave the daemon (remote escape hatch) alone"
print("1. --run: /stop + hardware owners freed before the script OK")

# 2. --restore: unmask BEFORE any start, full set started (incl. units
#    --run never stopped)
os.unlink(LOG)
r = subprocess.run(["bash", WRAPPER, "--restore"], env=env,
                   capture_output=True, text=True, timeout=30)
assert r.returncode == 0, r.stderr
calls = open(LOG).read().splitlines()
assert calls[0].startswith("unmask "), "unmask must run first (QA)"
started = [c.split()[1] for c in calls if c.startswith("start ")]
for unit in ("tapbox-ui", "tapbox-idle", "tapbox-buttons",
             "tapbox-daemon", "go-librespot", "tapbox-mpris",
             "tapbox-bt-reconnect"):
    assert unit in started, f"restore must start {unit}: {started}"
print("2. --restore: unmask first, full deterministic set OK")

# 3. a crashing extra: the wrapper execs the script, so its exit code IS
#    the unit result — systemd still runs ExecStopPost (pinned in
#    ui_extras 5); here we pin that the wrapper does not swallow it
bad = os.path.join(TMP, "bad.sh")
with open(bad, "w") as f:
    f.write("#!/bin/sh\nexit 7\n")
os.chmod(bad, 0o755)
r = subprocess.run(["bash", WRAPPER, "--run", bad], env=env,
                   capture_output=True, text=True, timeout=30)
assert r.returncode == 7, "the extra's exit must propagate (exec)"
print("3. crash propagates through exec (unit sees the failure) OK")

# 4. a dead daemon must not block the handoff (curl is best-effort)
srv.shutdown()
env_dead = dict(env, TAPBOX_DAEMON="http://127.0.0.1:9")
r = subprocess.run(["bash", WRAPPER, "--run", script], env=env_dead,
                   capture_output=True, text=True, timeout=30)
assert r.returncode == 0, r.stderr
print("4. handoff survives a dead daemon OK")

print("\nall extras_wrapper checks passed")
