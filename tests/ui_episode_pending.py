#!/usr/bin/env python3
"""Gate the hold-Y picker's pending path (architect review 2026-08-03).

The daemon's bounded settle can hand /expand a partial/EMPTY listing
with pending=true (cold 800-track context). The picker used to read
that as 'no list exists' and silently bail — hold-Y looked broken. Now:
one immediate re-fetch (the 'Fetching episodes ...' frame is already
up), and if STILL empty, an explicit message instead of silence. A
non-pending empty listing keeps the old quiet return (no list exists),
and a completed retry opens the picker."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402

ui.time.sleep = lambda s: None  # the message pause must not slow the gate

LIB = {"sections": [{"name": "S", "entries": [
    {"id": "pl1", "target": "spotify:album:x", "name": "Coco"}]}]}


def app(expansions):
    a = ui.App.__new__(ui.App)
    a.view = "now"
    a.sel = 0
    a.stack = []
    a.dirty = False
    a.status = {"target": "spotify:album:x"}
    a.library = LIB
    a.load_library = lambda ttl=0: None
    a.messages = []
    a.draw_message = lambda text, *k, **kw: a.messages.append(text)
    a.calls = []

    def fake_get(path, timeout=10):
        a.calls.append(path)
        return expansions[min(len(a.calls) - 1, len(expansions) - 1)]
    ui.api_get = fake_get
    return a


# 1. pending + empty twice: one retry, then an explicit message — never
#    a silent bail (hold-Y must not look dead)
PENDING = {"kind": "spotify", "episodes": [], "pending": True}
a = app([PENDING, PENDING])
a._open_episodes()
assert len(a.calls) == 2, f"exactly one retry expected: {a.calls}"
assert any("still loading" in m for m in a.messages), a.messages
assert a.view == "now", "must stay on now-playing"
print("1. pending twice: one retry + explicit message OK")

# 2. the retry completes: picker opens, no complaint
DONE = {"kind": "spotify", "pending": False, "episodes": [
    {"id": "spotify:track:a", "title": "A"}]}
a = app([PENDING, DONE])
a._open_episodes()
assert a.view == "episodes", f"picker must open: {a.view}"
assert not any("still loading" in m for m in a.messages), a.messages
print("2. retry completes: picker opens OK")

# 3. NOT pending + empty: the old quiet return (no list exists) — no
#    retry burned, no message
a = app([{"kind": "spotify", "episodes": [], "pending": False}])
a._open_episodes()
assert len(a.calls) == 1, f"no retry for a final empty listing: {a.calls}"
assert a.messages == ["Fetching episodes ..."], a.messages
assert a.view == "now"
print("3. final empty listing: quiet return, no retry OK")

print("\nall ui_episode_pending checks passed")
