#!/usr/bin/env python3
"""Gate the play-intent BT kick: pressing play (or any transport button)
while the configured output is a disconnected BT speaker must poke
btwatchd's kick file — an immediate connect attempt — instead of leaving
the kid waiting out the 20->300s blind-retry backoff after a boot where
the speaker came on late. No kick when the speaker is connected, and
never on the built-in output."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = STATE
os.environ["TAPBOX_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ.setdefault("TAPBOX_CACHE", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

KICK = daemon._bt.KICK_FILE


def set_output(device):
    with open(daemon.OUT_FILE, "w") as f:
        json.dump({"output": device, "pcm": "x"}, f)


def kicked():
    hit = os.path.exists(KICK)
    if hit:
        os.remove(KICK)
    return hit


# output = bt, speaker NOT connected
set_output("bt")
daemon._bt_transport_ready = lambda: False

# 1. a transport button kicks btwatchd (even with nothing to control)
daemon.ORCH.command("playpause")
assert kicked(), "playpause did not kick btwatchd"
print("1. playpause with disconnected speaker kicks a connect OK")

# 2. /play kicks too (spawn stubbed out — no real player process)
daemon.Orchestrator._spawn = lambda self, *a, **k: None
daemon.Orchestrator._stop_child = lambda self: None
daemon.ORCH.play("https://feeds.example.com/show")
assert kicked(), "play did not kick btwatchd"
print("2. play with disconnected speaker kicks a connect OK")

# 3. speaker already connected -> no kick (no churn on the radio)
daemon._bt_transport_ready = lambda: True
daemon.ORCH.command("playpause")
assert not kicked(), "kicked although the transport is up"
print("3. connected speaker is never kicked OK")

# 4. built-in output -> no kick, regardless of transport state
set_output("local")
daemon._bt_transport_ready = lambda: False
daemon.ORCH.command("playpause")
assert not kicked(), "kicked on the built-in output"
print("4. built-in output never kicks OK")

print("BT PLAY KICK OK — pressing play connects the speaker now, "
      "not after the backoff.")
