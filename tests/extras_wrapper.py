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

# fake systemctl: append verb+args to the log; is-enabled answers with
# the state from FAKE_ENABLED_STATE (default: healthy 'enabled')
FAKE = os.path.join(TMP, "systemctl")
with open(FAKE, "w") as f:
    f.write(f'#!/bin/sh\necho "$@" >> {LOG}\n'
            '[ "$1" = is-enabled ] && echo "${FAKE_ENABLED_STATE:-enabled}"\n'
            'exit 0\n')
os.chmod(FAKE, 0o755)

# fake daemon capturing the playback-stop call
STOPS = []


class Daemon(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            payload = {}
        STOPS.append((self.path, self.headers.get("Content-Type"), payload))
        out = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Daemon)
threading.Thread(target=srv.serve_forever, daemon=True).start()

# fake rfkill + iw: record the radio-baseline handback
RFKILL_LOG = LOG  # same log as systemctl so ORDER is assertable
FAKE_RFKILL = os.path.join(TMP, "rfkill")
with open(FAKE_RFKILL, "w") as f:
    f.write(f'#!/bin/sh\necho "rfkill $@" >> {LOG}\n')
os.chmod(FAKE_RFKILL, 0o755)
IW_LOG = os.path.join(TMP, "iw.log")
FAKE_IW = os.path.join(TMP, "iw")
with open(FAKE_IW, "w") as f:
    f.write(f'#!/bin/sh\necho "$@" >> {IW_LOG}\n')
os.chmod(FAKE_IW, 0o755)

# fake cpufreq tree: boot state is the powersave park
CPUS = os.path.join(TMP, "cpu")
for n in range(2):
    os.makedirs(os.path.join(CPUS, f"cpu{n}", "cpufreq"))
    with open(os.path.join(CPUS, f"cpu{n}", "cpufreq",
                           "scaling_governor"), "w") as f:
        f.write("powersave\n")


def governors():
    return [open(os.path.join(CPUS, f"cpu{n}", "cpufreq",
                              "scaling_governor")).read().strip()
            for n in range(2)]


env = dict(os.environ, TAPBOX_SYSTEMCTL=FAKE, TAPBOX_RFKILL=FAKE_RFKILL,
           TAPBOX_IW=FAKE_IW,
           TAPBOX_DAEMON=f"http://127.0.0.1:{srv.server_port}",
           TAPBOX_CPUFREQ=CPUS, TAPBOX_RUN=TMP)

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
assert STOPS and STOPS[0] == ("/stop", "application/json",
                              {"keep": True}), STOPS
# keep:true is the bookmark-preserving stop — a plain /stop wipes the
# kid's audiobook position on every extra launch (field 2026-07-29)
with open(MARK) as f:
    before_script = f.read()
assert "stop tapbox-idle tapbox-buttons tapbox-ui" in before_script
assert "stop go-librespot" in before_script, "the ALSA holder must stop"
assert "tapbox-daemon" not in before_script, \
    "--run must leave the daemon (remote escape hatch) alone"
assert governors() == ["ondemand", "ondemand"], \
    "the powersave park (600MHz) must lift for the extra"
print("1. --run: /stop + hardware freed + CPU unparked OK")

# 2. --restore: radios come back BEFORE any unit start (a blocked wifi
#    stalled the start queue 60s on network-online with the SCREEN
#    stuck behind it — field 2026-07-29), the screen starts first,
#    go-librespot goes async, and a healthy system needs NO
#    unmask/enable (each cost a ~2s daemon-reload)
os.unlink(LOG)
r = subprocess.run(["bash", WRAPPER, "--restore"], env=env,
                   capture_output=True, text=True, timeout=30)
assert r.returncode == 0, r.stderr
calls = open(LOG).read().splitlines()
first_start = next(i for i, c in enumerate(calls) if c.startswith("start "))
unblock = next(i for i, c in enumerate(calls)
               if c.startswith("rfkill unblock"))
assert unblock < first_start, "radios must return before any unit starts"
assert calls[first_start] == "start tapbox-ui", \
    f"the screen must be the first unit back: {calls[first_start]}"
started = [c.split()[-1] for c in calls if c.startswith("start")]
for unit in ("tapbox-ui", "tapbox-idle", "tapbox-buttons",
             "tapbox-daemon", "go-librespot", "tapbox-mpris",
             "tapbox-bt-reconnect", "bluetooth", "bluealsa"):
    assert unit in started, f"restore must start {unit}: {started}"
assert "start --no-block go-librespot" in calls, \
    "go-librespot must not block the restore on network-online"
assert not any(c.startswith(("unmask", "enable")) for c in calls), \
    "healthy units must not be re-enabled (daemon-reload cost)"
assert "tapbox-btsnoop" not in " ".join(calls), \
    "the opt-in snoop ring must stay however the owner left it"
assert governors() == ["powersave", "powersave"], \
    "--restore must re-park the CPU to the snapshotted mode"
assert "dev wlan0 set txpower auto" in open(IW_LOG).read(), \
    "--restore must undo a script's fixed txpower softening"
# a MASKED unit (contract breach) is still healed — unmask + enable
os.unlink(LOG)
subprocess.run(["bash", WRAPPER, "--restore"],
               env=dict(env, FAKE_ENABLED_STATE="masked"),
               capture_output=True, timeout=30)
calls = open(LOG).read().splitlines()
assert any(c.startswith("unmask tapbox-ui") for c in calls)
assert any(c.startswith("enable tapbox-ui") for c in calls)
# ...and the snapshot honors a box that was in perf mode before the game
for n in range(2):
    with open(os.path.join(CPUS, f"cpu{n}", "cpufreq",
                           "scaling_governor"), "w") as f:
        f.write("ondemand\n")
subprocess.run(["bash", WRAPPER, "--run", script], env=env,
               capture_output=True, timeout=30)
subprocess.run(["bash", WRAPPER, "--restore"], env=env,
               capture_output=True, timeout=30)
assert governors() == ["ondemand", "ondemand"], \
    "a perf-mode box must return to perf, not be forced to powersave"
print("2. --restore: radios-first, screen-first, async spotify, lazy heal OK")

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

# 3b. the screen-message contract: a FAILED unit leaves a generic note
#     for tapbox-ui unless the script wrote its own; success leaves
#     nothing; --run clears last time's leftover
MSG = os.path.join(TMP, "tapbox-extra.msg")
env_fail = dict(env, SERVICE_RESULT="exit-code", EXIT_STATUS="1")
subprocess.run(["bash", WRAPPER, "--restore"], env=env_fail,
               capture_output=True, timeout=30)
assert "Extra failed (1)" in open(MSG).read(), \
    "a silent failure must leave a word for the screen"
os.unlink(MSG)
with open(MSG, "w") as f:
    f.write("RetroPie: no TV found\n")
subprocess.run(["bash", WRAPPER, "--restore"], env=env_fail,
               capture_output=True, timeout=30)
assert open(MSG).read().strip() == "RetroPie: no TV found", \
    "a script's own message must never be overwritten"
subprocess.run(["bash", WRAPPER, "--run", script], env=env,
               capture_output=True, timeout=30)
assert not os.path.exists(MSG), "--run must clear a stale note"
subprocess.run(["bash", WRAPPER, "--restore"], env=env,
               capture_output=True, timeout=30)
assert not os.path.exists(MSG), "a clean exit leaves no note"
print("3b. screen-message contract: generic on failure, script's own "
      "kept, stale cleared OK")

# 4. a dead daemon must not block the handoff (curl is best-effort)
srv.shutdown()
env_dead = dict(env, TAPBOX_DAEMON="http://127.0.0.1:9")
r = subprocess.run(["bash", WRAPPER, "--run", script], env=env_dead,
                   capture_output=True, text=True, timeout=30)
assert r.returncode == 0, r.stderr
print("4. handoff survives a dead daemon OK")

print("\nall extras_wrapper checks passed")
