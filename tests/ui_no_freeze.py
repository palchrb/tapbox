#!/usr/bin/env python3
"""Gate the UI's freeze defenses. (1) Render-path daemon polls must use
a SHORT timeout: the render loop is single-threaded, so a daemon slowed
by go-librespot's blocking API (a track load stalls its whole HTTP
layer — field 2026-07-20 12:54) must never freeze the screen more than
a beat. (2) A render-loop watchdog restarts tapbox-ui if the loop ever
wedges for real, instead of the kid staring at a frozen frame until the
60-min idle shutdown — but it must NOT fire during a legitimate long
inline block (a ~130s BT pair)."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")

import ui  # noqa: E402


# --- 1. render-path timeouts -------------------------------------------------
CALLS = []


def fake_get(path, timeout=10):
    CALLS.append((path, timeout))
    return {"sections": []}


ui.api_get = fake_get

# load_library must ask with the short render timeout, not the 10s default
lib_stub = type("S", (), {"_lib_at": 0.0, "library": {}})()
ui.App.load_library(lib_stub)
paths = dict(CALLS)
assert paths.get("/library") == ui.RENDER_HTTP_TIMEOUT, CALLS
print("1. /library uses the short render timeout OK")

# and the guard: the render timeout stays small — a regression back to
# the 10s default is exactly what froze the screen
assert ui.RENDER_HTTP_TIMEOUT <= 3.0, ui.RENDER_HTTP_TIMEOUT
print("2. render HTTP timeout is small (<=3s) OK")

# refresh(): /system and /settings must carry the short timeout too
CALLS.clear()


class RefreshStub:
    last_system = -1e9
    view = "carousel"
    last_status = 0.0
    status = {}
    settings = {}

    def _set(self, attr, value):
        setattr(self, attr, value)

    class display:
        @staticmethod
        def set_brightness(_pct):
            pass

    def _apply_nav_mode(self):
        pass


rs = RefreshStub()
ui.App.refresh(rs)
poll = {p: t for p, t in CALLS}
assert poll.get("/system") == ui.RENDER_HTTP_TIMEOUT, CALLS
assert poll.get("/settings") == ui.RENDER_HTTP_TIMEOUT, CALLS
print("3. refresh() polls /system + /settings with the short timeout OK")


# --- 2. the render-loop watchdog --------------------------------------------
class Exited(Exception):
    pass


class StopLoop(Exception):
    pass


def run_watchdog(beat_age, threshold=ui.UI_WATCHDOG_S):
    """Drive one watchdog check: _loop_beat aged beat_age seconds ago.
    Returns True if it would restart the UI (os._exit called)."""
    wd = type("W", (), {"_loop_beat": ui.time.monotonic() - beat_age})()
    real_exit, real_sleep, real_wd = ui.os._exit, ui.time.sleep, ui.UI_WATCHDOG_S
    fired = [False]

    def fake_exit(_code):
        fired[0] = True
        raise Exited

    ticks = [0]

    def fake_sleep(_s):
        ticks[0] += 1
        if ticks[0] > 1:
            raise StopLoop  # one check per run

    ui.os._exit = fake_exit
    ui.time.sleep = fake_sleep
    ui.UI_WATCHDOG_S = threshold
    try:
        ui.App._render_watchdog(wd)
    except (Exited, StopLoop):
        pass
    finally:
        ui.os._exit, ui.time.sleep, ui.UI_WATCHDOG_S = \
            real_exit, real_sleep, real_wd
    return fired[0]


# 4. a healthy loop (beat just now) is never restarted
assert run_watchdog(beat_age=1, threshold=180) is False
print("4. a fresh heartbeat never trips the watchdog OK")

# 5. a wedged loop (beat older than the threshold) restarts the UI
assert run_watchdog(beat_age=200, threshold=180) is True
print("5. a stalled render loop restarts the UI OK")

# 6. a legitimate long inline block (a ~130s BT pair) is BELOW the
# threshold, so a parent mid-pairing is never killed
assert run_watchdog(beat_age=132, threshold=180) is False
print("6. a 130s BT pair stays under the watchdog threshold OK")

print("UI NO FREEZE OK — a slow daemon can't freeze the screen, and a "
      "truly wedged render loop self-heals without killing long BT ops.")
