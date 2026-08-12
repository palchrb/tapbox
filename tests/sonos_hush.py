#!/usr/bin/env python3
"""The silent Play->Seek window. Field 2026-08-12: resuming on Sonos
audibly played ~1s from 0:00 before jumping to the saved position —
Seek needs a PLAYING transport (UPnP 701 against STOPPED), so the
order SetURI -> Play -> Seek is forced and the blip is structural.
_hush() mutes the speaker across that window and puts the OLD mute
state back afterwards. Pins: mute set before Play and restored after
Seek, an already-muted speaker stays muted, start<5 never touches
mute, and a mid-flight failure still restores (no stuck-muted box)."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()

import sonosd  # noqa: E402


class FakeTransport:
    def __init__(self, log, fail_seturi=False):
        self._log = log
        self._fail = fail_seturi

    def SetAVTransportURI(self, args):
        if self._fail:
            raise OSError("speaker went away")
        self._log.append("seturi")

    def Play(self, args):
        self._log.append("play")

    def GetTransportInfo(self, args):
        return {"CurrentTransportState": "PLAYING"}

    def Seek(self, args):
        self._log.append(("seek", dict(args)["Target"]))


class FakeSpk:
    def __init__(self, log, muted=False, fail_seturi=False):
        self._log = log
        self._muted = muted
        self.avTransport = FakeTransport(log, fail_seturi)

    @property
    def mute(self):
        return self._muted

    @mute.setter
    def mute(self, v):
        self._muted = bool(v)
        self._log.append(("mute", bool(v)))

    def clear_queue(self):
        pass

    def play_from_queue(self, idx):
        self._log.append(("jump", idx))


def session(spk):
    s = sonosd.Session()
    s._spk = lambda: spk
    return s


BODY = {"uid": "RINCON_X", "kind": "url", "uri": "http://c/e.mp3"}

# 1. a resume: mute BEFORE Play, Seek to the target, restore AFTER
log = []
spk = FakeSpk(log)
r = session(spk).play(dict(BODY, start_s=120))
assert r["ok"] and r["sought"] is True
assert log == [("mute", True), "seturi", "play",
               ("seek", "0:02:00"), ("mute", False)], log
assert spk.mute is False
print("1. resume: muted across the Play->Seek window, then back OK")

# 2. an already-muted speaker STAYS muted (restore = old state)
log = []
spk = FakeSpk(log, muted=True)
session(spk).play(dict(BODY, start_s=60))
assert log[0] == ("mute", True) and log[-1] == ("mute", True)
assert spk.mute is True
print("2. deliberately muted speaker stays muted OK")

# 3. a from-the-top play (start<5) never touches mute
log = []
spk = FakeSpk(log)
r = session(spk).play(dict(BODY))
assert r["sought"] is True
assert not any(isinstance(e, tuple) and e[0] == "mute" for e in log), log
print("3. start<5: mute untouched OK")

# 4. SetURI dies mid-flight: play() raises but the mute is RESTORED —
#    a network blip must never leave the speaker stuck silent
log = []
spk = FakeSpk(log, fail_seturi=True)
try:
    session(spk).play(dict(BODY, start_s=120))
    raise AssertionError("play() should have raised")
except OSError:
    pass
assert log == [("mute", True), ("mute", False)], log
assert spk.mute is False
print("4. failure path restores mute — no stuck-silent speaker OK")

# 5. queue_play (episode/track jump with a saved position) hushes too
log = []
spk = FakeSpk(log)
s = session(spk)
s.uid = "RINCON_X"
s.q_base, s.q_len = 1, 10
r = s.verb("queue_play", {"index": 2, "start_s": 45})
assert r["ok"] and r["sought"] is True
assert log == [("mute", True), ("jump", 2),
               ("seek", "0:00:45"), ("mute", False)], log
print("5. queue jump with resume hushes the window too OK")

print("\nSONOS HUSH OK — the second from 0:00 still happens, silently.")
