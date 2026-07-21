#!/usr/bin/env python3
"""Gate go-librespot's v0.0.7 LIVE output reopen (POST /player/output).
Its audio_device used to be startup-only config, so every built-in<->bt
switch was a config rewrite + `systemctl restart` that killed the Spotify
session mid-song and re-burst the shared 2.4GHz radio. v0.0.7 reopens the
ALSA output on a running process, keeping track/position/volume/paused.
Contract: reopen_go_output persists the device for the next boot AND
reopens it live, returning True; on a pre-v0.0.7 binary (the endpoint
404s -> HTTPError, an OSError) it returns False so the caller falls back
to the restart path."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
CFG = os.path.join(tempfile.mkdtemp(), "config.yml")
os.environ["TAPBOX_GO_CONFIG"] = CFG
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import output, spotify  # noqa: E402

with open(CFG, "w") as f:
    f.write("bitrate: 320\naudio_device: tapbox_local\nvolume: 60\n")

# 1. _write_audio_device rewrites the line in place, once
assert output._write_audio_device("tapbox_bt") is True
assert "audio_device: tapbox_bt" in open(CFG).read()
assert "bitrate: 320" in open(CFG).read(), "must not clobber other keys"
assert output._write_audio_device("tapbox_bt") is False, "unchanged -> no write"
print("1. _write_audio_device: in-place, idempotent, keeps siblings OK")

# 2. missing key -> appended (never silently dropped)
with open(CFG, "w") as f:
    f.write("bitrate: 320\n")
assert output._write_audio_device("tapbox_local") is True
assert "audio_device: tapbox_local" in open(CFG).read()
print("2. _write_audio_device: appends a missing key OK")

# 3. reopen success: persists the device AND calls the live endpoint
CALLS = []
spotify.go = lambda path, timeout=5, body=None: CALLS.append((path, body)) or b"{}"
assert output.reopen_go_output("tapbox_bt") is True
assert "audio_device: tapbox_bt" in open(CFG).read(), "persist for next boot"
assert CALLS == [("/player/output", {"device": "tapbox_bt"})], CALLS
print("3. reopen_go_output: persists config + POST /player/output OK")

# 4. pre-v0.0.7 binary: a 404 (urllib HTTPError IS an OSError) -> False so
# the caller falls back to the config-rewrite + restart path
def _old(path, timeout=5, body=None):
    raise OSError("HTTP 404 — endpoint absent")

spotify.go = _old
assert output.reopen_go_output("tapbox_local") is False, "fall back on 404"
# the persist still happened (harmless; startup config only)
assert "audio_device: tapbox_local" in open(CFG).read()
print("4. reopen_go_output: returns False on an old binary (fall back) OK")

# 5. no GO_CONFIG configured: persist is a no-op but the live reopen is
# still attempted (a running process has its own device to reopen)
os.environ.pop("TAPBOX_GO_CONFIG", None)
import importlib  # noqa: E402
importlib.reload(output)
CALLS.clear()
spotify.go = lambda path, timeout=5, body=None: CALLS.append((path, body)) or b"{}"
assert output.reopen_go_output("tapbox_bt") is True
assert CALLS == [("/player/output", {"device": "tapbox_bt"})], CALLS
print("5. reopen_go_output: no config file still reopens the live output OK")

print("OUTPUT REOPEN OK — v0.0.7 switches output live (session kept, no "
      "restart, no radio burst); an old binary cleanly falls back.")
