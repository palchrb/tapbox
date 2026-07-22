#!/usr/bin/env python3
"""Gate the optimistic play/pause icon (P1 follow-up).

With P1, /status is fetched by the background poller, so the now-view
play/pause ICON lagged the MUSIC (which responds to the immediate control
POST) by a poll cycle — worse when the daemon is slow. Fix: flip the icon
locally on press, and hold that state until a poll CONFIRMS it (or a short
window expires) so a stale go-librespot report can't flicker it back."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")

import ui  # noqa: E402


class FakeDisplay:
    on = True
    def show(self, img): pass
    def set_backlight(self, on): pass
    def set_brightness(self, b): pass


ui.api_post = lambda *a, **k: {}  # control POST is a no-op here

app = ui.App(FakeDisplay(), None)
app.status = {"playing": True}

# 1. pressing A flips the icon optimistically, at once
app.handle_now("a")
assert app.status.get("playing") is False, \
    "play/pause must flip the icon optimistically on press"
assert app._pp_expect is False
print("1. play/pause flips the icon optimistically on press OK")

# 2. a STALE /status (go-librespot still reports playing) must NOT flicker
#    the icon back — the optimistic value is held
app._set("status", {"playing": True, "title": "X"})
assert app.status.get("playing") is False, \
    "a stale report must not undo the optimistic flip"
assert app._pp_expect is False  # still pending confirmation
print("2. a stale /status does not flicker the icon back OK")

# 3. a CONFIRMING /status hands over to real status and clears the hold
app._set("status", {"playing": False, "title": "X", "position": 5})
assert app.status.get("playing") is False
assert app.status.get("position") == 5  # real fields still flow through
assert app._pp_expect is None
print("3. a confirming /status clears the optimistic hold OK")

# 4. after the reconcile window expires, real status wins even if it
#    contradicts (never stick on a wrong guess forever)
app.status = {"playing": True}
app.handle_now("a")              # optimistic -> paused
app._pp_until = ui.time.monotonic() - 1   # force the window expired
app._set("status", {"playing": True, "title": "Y"})
assert app.status.get("playing") is True, "after the window, real status wins"
assert app._pp_expect is None
print("4. after the reconcile window, real status wins OK")

print("\nall ui_playpause_optimistic checks passed")
