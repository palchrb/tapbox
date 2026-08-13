#!/usr/bin/env python3
"""Gate P1: daemon HTTP lives on a background poller, never on the render/
input loop, so a slow /status (behind go-librespot's blocking API during a
track load) can't stall the button->repaint path. And it must honor the
power invariant: NO HTTP while the screen is dark.

Invariants:
  - _reconcile_view (the main-loop view step) touches no network.
  - the poller does zero HTTP while display.on is False (no polling all night).
  - a wake kick un-parks the poller and it fetches immediately."""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")

import ui  # noqa: E402


class FakeDisplay:
    def __init__(self): self.on = True
    def show(self, img): pass
    def set_backlight(self, on): self.on = on
    def set_brightness(self, b): pass


calls = []


def rec_get(path, timeout=10):
    calls.append(path)
    return {"sections": []} if path == "/library" else {}


ui.api_get = rec_get

app = ui.App(FakeDisplay(), None)
app.view = "cats"
app.last_system = -1e9
app.last_status = 0.0
app.user_touched = False
app.status = {"playing": False}
app.settings = {"simple_nav": 2}

# 1. the main-loop reconcile never touches HTTP (that's the whole point —
#    a slow daemon can't stall a repaint). Run it BEFORE the poller starts.
calls.clear()
ui.App._reconcile_view(app)
assert calls == [], f"_reconcile_view must do no HTTP, got {calls}"
print("1. _reconcile_view (main loop) does zero HTTP OK")

# 2. the poller does ZERO HTTP while the screen is dark (power invariant:
#    no 1/s status + bluealsa fork all night)
app.display.on = False
threading.Thread(target=app._poller, daemon=True).start()
time.sleep(0.4)
assert calls == [], f"poller must not HTTP while dark, got {calls}"
print("2. poller does no HTTP while the screen is dark OK")

# 3. a wake kick un-parks the poller and it fetches immediately
app.display.on = True
app._poll_wake.set()
time.sleep(0.4)
assert "/status" in calls, f"a wake kick must make the poller fetch, got {calls}"
assert "/library" in calls, f"the poller keeps /library warm, got {calls}"
print("3. a wake kick un-parks the poller and it fetches OK")

# 4. re-parking (screen dark again) stops the HTTP
app.display.on = False
time.sleep(0.4)  # let it finish any in-flight tick and re-park
calls.clear()
time.sleep(0.4)
assert calls == [], f"poller must re-park when the screen goes dark, got {calls}"
print("4. poller re-parks (no HTTP) when the screen goes dark again OK")

print("\nall ui_poller checks passed")
