#!/usr/bin/env python3
"""Gate keep-audio-alive on BT transport death (rig follow-up 2026-07-22:
two hci0 hardware-error crashes in one evening mid-podcast).

When the A2DP transport dies under a playing mpv and the box HAS a built-in
card, retarget mpv live to the local device and KEEP PLAYING instead of
stopping — a silent box mid-story reads as broken to a kid. Crucially the
blip machinery must NOT be armed then (audio never stopped, so a returning
transport must not fire a second starter). No card / IPC dead -> the old
stop+bookmark+popup behavior stands."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_STATE"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(os.environ["TAPBOX_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
daemon.current_output = lambda **k: {"output": "bt"}
orch._mpv_alive = lambda: True
STOPPED = []
orch._stop_child = lambda: STOPPED.append(1)

# 1. built-in card + live mpv IPC -> retarget to local, keep playing,
#    NO stop, and the blip machinery is NOT armed
daemon._i2s_card_present = lambda: True
sent = []
daemon.mpv_ipc = lambda cmd: (sent.append(cmd), {"error": "success"})[1]
with daemon._BT_WAIT_LOCK:
    daemon._BT_WAIT.update(lost=0.0, since=0.0, ready_until=0.0)
r = daemon._bt_transport_lost()
assert r == {"stopped": False, "kept": "local"}, r
assert not STOPPED, "must keep playing, not stop"
assert sent and sent[0][:2] == ["set_property", "audio-device"], sent
assert "tapbox_local" in sent[0][2], sent
with daemon._BT_WAIT_LOCK:
    assert daemon._BT_WAIT["lost"] == 0.0, \
        "blip machinery must NOT arm when audio kept playing"
print("1. transport death -> mpv retargets to built-in, keeps playing, "
      "no blip arm OK")

# 2. no built-in card -> the old stop+bookmark+popup behavior stands
daemon._i2s_card_present = lambda: False
sent.clear()
r = daemon._bt_transport_lost()
assert r == {"stopped": True} and STOPPED, (r, STOPPED)
with daemon._BT_WAIT_LOCK:
    assert daemon._BT_WAIT["lost"] > 0.0, "popup path must still arm"
print("2. no built-in card -> old stop + popup behavior stands OK")

# 3. card present but mpv IPC dead -> fall through to stop (never a silent
#    zombie that neither plays nor stops)
STOPPED.clear()
with daemon._BT_WAIT_LOCK:
    daemon._BT_WAIT.update(lost=0.0)
daemon._i2s_card_present = lambda: True


def _boom(cmd):
    raise OSError("ipc gone")


daemon.mpv_ipc = _boom
r = daemon._bt_transport_lost()
assert r == {"stopped": True} and STOPPED, (r, STOPPED)
print("3. dead mpv IPC -> falls back to stop (no silent zombie) OK")

print("\nall bt_lost_keep_playing checks passed")
