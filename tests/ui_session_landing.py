#!/usr/bin/env python3
"""Where the screen opens at power-on (session design 2026-08-13).

The daemon keeps serving a ghost card for the remembered entry — that
is a 2026-08-10 field fix and must NOT be suppressed — so the SCREEN is
what has to know the difference: a session inside the resume window
opens on now-playing (carry on where we were), an expired one opens on
the browse root with the remembered tile selected (wake up in the
carousel). A daemon that hasn't said yet, or is too old to have the
field at all, keeps today's behaviour."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
os.environ["TAPBOX_EMOJI"] = "0"

import ui  # noqa: E402


class FakeDisplay:
    on = True

    def show(self, img):
        pass

    def set_backlight(self, on):
        pass

    def set_brightness(self, b):
        pass


class FakeInputs:
    def poll(self, timeout):
        return []


T1, T2 = "https://x/one", "https://x/two"
LIB = {"sections": [
    {"id": "s1", "name": "Musikk", "entries": [
        {"id": "e0", "name": "Album A", "target": T1},
        {"id": "e1", "name": "Album B", "target": T2}]},
]}


def app_with(status, nav):
    app = ui.App(FakeDisplay(), FakeInputs())
    app.library = LIB
    app._lib_at = time.monotonic() + 999
    app.settings = {"simple_nav": nav}
    app.status = status
    app._boot_landing()
    return app


GHOST = {"target": T2, "title": "Sang 3", "playing": False}

# 1. text menus (nav 0): fresh lands in now-playing, expired at home
assert app_with(dict(GHOST, session="fresh"), 0).view == "now"
a = app_with(dict(GHOST, session="expired"), 0)
assert a.view != "now" and a.stack == []
print("1. nav 0: fresh -> now playing, expired -> menu OK")

# 2. flat carousel (nav 1): expired opens ON the carousel, with the
#    remembered tile selected — "you were here" without being inside it
a = app_with(dict(GHOST, session="expired"), 1)
assert a.view == "carousel" and a.stack == []
assert a.car_sel == 1, "the remembered album must still be the selected tile"
a = app_with(dict(GHOST, session="fresh"), 1)
assert a.view == "now" and a.stack == [("carousel", 0)]
assert a.car_sel == 1, "back from now-playing must return to that tile"
print("2. nav 1: expired rests on the remembered tile, fresh opens it OK")

# 3. category carousel (nav 2): same rule, and the category is entered
a = app_with(dict(GHOST, session="expired"), 2)
assert a.view == "carousel" and a.car_section == "s1"
assert a.view != "now"
print("3. nav 2: expired lands in the remembered category, not inside OK")

# 4. missing field (older daemon mid-upgrade) = today's behaviour
assert app_with(dict(GHOST), 0).view == "now"
# 5. 'pending' is not 'expired': the daemon is still judging, and the
#    splash loop above has already waited — never punish that with a
#    wrong screen
assert app_with(dict(GHOST, session="pending"), 0).view == "now"
print("4+5. missing/pending session keeps the old landing OK")

# 6. nothing remembered at all: the root, whatever the session says
a = app_with({"session": "fresh"}, 1)
assert a.view == "carousel" and a.stack == []
print("6. an empty box lands on the browse root OK")

print("\nUI SESSION LANDING OK — carry on where we were, or wake up "
      "in the carousel.")
