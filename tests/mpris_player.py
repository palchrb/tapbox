#!/usr/bin/env python3
"""Gate the AVRCP media-player bridge (tapbox-mpris).

Born from the 2026-07-27 btsnoop capture: the Skoda head unit polls the
AVRCP player list continuously while streaming, and with no player
registered BlueZ answers every round with Invalid Player ID — endless
control-channel chatter during live A2DP, the known channel-ops-while-
streaming crasher on this chip. The bridge also puts title/artist on
the car display and routes the car's buttons to the daemon's IDEMPOTENT
endpoints (the 937ea05 rule: an explicit play must never pause).

Tested pure-python (no dbus): the mapping and routing logic is what can
be wrong; the dbus glue is a thin shell around it."""
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
sys.path.insert(0, os.path.join(REPO, "pi"))

import mpris  # noqa: E402

# 1. status -> MPRIS properties: the spotify shape
st = {"title": "Ya Ya Ya", "playing": True, "position": 42.5,
      "duration": 169.2,
      "spotify": {"track": "Ya Ya Ya", "artists": ["JONAS LOVV"],
                  "album": "Ya Ya Ya"}}
p = mpris.status_to_props(st)
assert p["PlaybackStatus"] == "Playing"
assert p["Metadata"]["xesam:title"] == "Ya Ya Ya"
assert p["Metadata"]["xesam:artist"] == ["JONAS LOVV"]
assert p["Metadata"]["xesam:album"] == "Ya Ya Ya"
assert p["Metadata"]["mpris:length"] == 169_200_000  # µs
assert p["Position"] == 42_500_000
print("1. spotify status maps to xesam metadata (µs units) OK")

# 1b. a podcast (no spotify block) still gets a title; artists from a
#     stale spotify dict must NOT leak onto an mpv track
st2 = {"title": "Vi er kule og vi er rare", "playing": True,
       "position": 30, "duration": 600,
       "spotify": {"track": "Something Else", "artists": ["Wrong Guy"]}}
p2 = mpris.status_to_props(st2)
assert p2["Metadata"]["xesam:title"] == "Vi er kule og vi er rare"
assert "xesam:artist" not in p2["Metadata"], \
    "another source's artists must not label a podcast"
print("1b. podcast: title mapped, stale spotify artists don't leak OK")

# 1c. paused keeps the card (the car shows what WOULD play); nothing
#     loaded reads Stopped
assert mpris.status_to_props({"title": "X", "playing": False}
                             )["PlaybackStatus"] == "Paused"
assert mpris.status_to_props({})["PlaybackStatus"] == "Stopped"
print("1c. Playing/Paused/Stopped mapping OK")

# 2. THE COMMAND TABLE: the car's explicit commands must hit the
#    idempotent endpoints — Play may NEVER route to the toggle
assert mpris.COMMANDS["Play"] == "/resume", \
    "an explicit AVRCP play must be idempotent (937ea05)"
assert mpris.COMMANDS["Pause"] == "/pause"
assert mpris.COMMANDS["Stop"] == "/pause", \
    "ignition-off Stop must not drop the queue/bookmark"
assert mpris.COMMANDS["PlayPause"] == "/playpause"
assert mpris.COMMANDS["Next"] == "/next"
assert mpris.COMMANDS["Previous"] == "/prev"
print("2. command table: explicit commands stay idempotent OK")

# 2b. ...and post() forwards through boxapi (which carries the token)
sent = []
fake_boxapi = types.SimpleNamespace(
    post=lambda path, body=None, **k: sent.append(path))
fake_pkg = types.SimpleNamespace(boxapi=fake_boxapi)
sys.modules["tapbox"] = fake_pkg
sys.modules["tapbox.boxapi"] = fake_boxapi
mpris.post(mpris.COMMANDS["Play"])
assert sent == ["/resume"], sent
print("2b. post() forwards to the daemon via boxapi OK")

# 3. change detection: steady playback emits NOTHING (BlueZ extrapolates
#    position itself; per-tick spam just wakes the AVRCP machinery)
old = mpris.status_to_props(st)
tick = dict(st, position=st["position"] + mpris.POLL_S)  # one poll later
new = mpris.status_to_props(tick)
assert mpris.props_changed(old, new) == {}, \
    "steady playback must not emit PropertiesChanged"
print("3. steady playback: no PropertiesChanged spam OK")

# 3b. a SEEK (position off the extrapolation) does emit Position
jump = dict(st, position=st["position"] + 60)
changed = mpris.props_changed(old, mpris.status_to_props(jump))
assert "Position" in changed and changed.get("Metadata") is None, changed
print("3b. a seek emits Position OK")

# 3c. a track change emits Metadata (this is what updates the car's
#     display), and carries the fresh position
nxt = {"title": "Neste sang", "playing": True, "position": 0,
       "duration": 200, "spotify": {"track": "Neste sang",
                                    "artists": ["Artist"]}}
changed = mpris.props_changed(old, mpris.status_to_props(nxt))
assert changed["Metadata"]["xesam:title"] == "Neste sang"
assert "Position" in changed
print("3c. track change emits Metadata + Position OK")

# 3d. pause/resume emits PlaybackStatus
paused = dict(st, playing=False)
changed = mpris.props_changed(old, mpris.status_to_props(paused))
assert changed["PlaybackStatus"] == "Paused", changed
print("3d. pause emits PlaybackStatus OK")

# 4. first poll (no previous state) announces everything
first = mpris.props_changed(None, new)
assert first["PlaybackStatus"] and "Metadata" in first
print("4. first poll announces the full state OK")

print("\nall mpris_player checks passed")
