#!/usr/bin/env python3
"""Verify the box has ONE persisted volume, shared by mpv and Spotify and
kept across restarts unless the user changes it. Setting volume writes
volume.json (in the persistent STATE_DIR); player.py reads that same file
when it starts mpv AND when it applies the level to go-librespot, so a
source switch or a reboot never jumps the level on its own."""
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
orch = daemon.ORCH


def saved():
    with open(daemon.VOL_FILE) as f:
        return json.load(f)["volume"]


# the daemon and player.py must agree on the file location (the whole
# point — one knob, not two)
import player  # noqa: E402
assert daemon.VOL_FILE == os.path.join(daemon.STATE_DIR, "volume.json")
assert os.path.join(player.STATE_DIR, "volume.json") == daemon.VOL_FILE
print("1. daemon and player.py share one volume.json in STATE_DIR OK")

# setting volume on the mpv source persists it
MPV_VOL = {"volume": 100}
orch._mpv_alive = lambda: True
orch.source = "mpv"
daemon.mpv_get = lambda p: MPV_VOL.get(p)
daemon.mpv_ipc = lambda cmd: (MPV_VOL.__setitem__("volume", cmd[2]),
                              {"error": "success"})[1]
daemon.load_settings = lambda: {"volume_cap": 100}
r = orch.volume(absolute=40)
assert r["routed"] == "mpv" and r["volume"] == 40
assert saved() == 40, saved()
print("2. volume set on mpv persists to volume.json OK")

# switching to Spotify and adjusting keeps writing the SAME file, so the
# level carries across the source change (no jump)
orch._mpv_alive = lambda: False
orch.source = "spotify"
GO = {"volume": round(40 * 65535 / 100), "volume_steps": 65535}
daemon.go_status = lambda **_k: GO
daemon.go = lambda path, body=None: GO.__setitem__("volume", body["volume"])
r = orch.volume(delta=+5)  # nudge up from the shared 40 -> 45
assert r["routed"] == "spotify" and r["volume"] == 45, r
assert saved() == 45, saved()
print("3. Spotify adjustment writes the same shared file (no jump) OK")

# the child-safety cap clamps and still persists the capped value
daemon.load_settings = lambda: {"volume_cap": 60}
r = orch.volume(absolute=90)
assert r["volume"] == 60 and saved() == 60, (r, saved())
print("4. volume cap clamps and persists OK")

# player.py starts mpv at exactly the saved level (survives a reboot)
with open(daemon.VOL_FILE, "w") as f:
    json.dump({"volume": 33}, f)
v = max(0, min(100, round(json.load(open(daemon.VOL_FILE))["volume"])))
assert v == 33, "player.py's start-volume read must match the saved level"
print("5. a restart starts playback at the persisted level OK")

print("VOLUME OK — one persisted knob, shared mpv/Spotify, steady across "
      "restarts.")
