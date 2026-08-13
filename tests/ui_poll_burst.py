#!/usr/bin/env python3
"""Kick-at-send + the /status burst. Field 2026-08-13: after a skip the
poller waited for the POST to return before refetching, and then fell
back to the 1s idle cadence — the new track's title landed up to a
second late even though the daemon knew it. Now _control_async kicks
the poller the moment the command is SENT and opens a 3s burst window
(poll_burst_until) where /status refetches every BURST_POLL_S=0.3s,
measured from fetch COMPLETION so a slow daemon self-paces (QA
2026-08-13, pitfall 1). Dedicated deadline, NOT catch_up_until —
extending that would force identical-frame repaints at 5fps (QA
pitfall 8)."""
import os
import sys
import tempfile
import threading
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


# 1. kick at SEND: the poller is woken and the burst window opened
#    BEFORE the POST returns (the POST here blocks until released)
release = threading.Event()
posted = []


def slow_post(path, body=None, timeout=None):
    posted.append(path)
    release.wait(5)
    return {}


ui.api_post = slow_post
app = ui.App(FakeDisplay(), FakeInputs())
app.last_status = 99.0
app._poll_wake.clear()
before = time.monotonic()
app._control_async("/next")
assert app.last_status == 0.0, "kick must force an immediate refetch"
assert app._poll_wake.is_set(), "kick must wake the poller"
assert app.poll_burst_until > before + 2, "burst window must be open"
assert app.catch_up_until <= before, \
    "a skip must NOT extend catch_up_until (identical-frame repaints)"
release.set()
for _ in range(50):
    if posted:
        break
    time.sleep(0.05)
assert posted == ["/next"]
print("1. kick at send: wake + burst before the POST returns OK")

# 2. burst cadence: /status refetched at BURST_POLL_S inside the
#    window, 1s outside — driven by a scripted clock
calls = []
real_get, real_mono = ui.api_get, time.monotonic
now = [1000.0]
app.display.on = False   # park the REAL poller thread (skips HTTP when
#                          dark) — sections below drive _poll_once by hand
try:
    ui.time.monotonic = lambda: now[0]

    def fake_get(path, timeout=None):
        if path != "/status":      # /library etc: free and uncounted
            return {"sections": []}
        calls.append((round(now[0], 2), path))
        return {"target": "t", "playing": True}

    ui.api_get = fake_get
    app.view = "now"
    app.status = {}
    app.library = {"sections": []}
    app._lib_at = 10**9           # load_library stays quiet
    app.last_system = 10**9       # /system poll stays quiet
    app.poll_burst_until = now[0] + 3
    app.last_status = now[0]

    def status_calls():
        return [c for c in calls if c[1] == "/status"]

    app._poll_once()
    assert not status_calls(), "gap 0 < BURST_POLL_S: no fetch yet"
    now[0] += 0.2
    app._poll_once()
    assert not status_calls(), "0.2s < 0.3s threshold: still no fetch"
    now[0] += 0.2                  # gap now 0.4 > 0.3
    app._poll_once()
    assert len(status_calls()) == 1, "burst fetch due at ~0.4s"

    # 3. completion stamp: make the fetch itself take 0.5s — the next
    #    fetch must count from completion, not from fetch start
    def slow_get(path, timeout=None):
        if path != "/status":      # only /status burns time here
            return {"sections": []}
        calls.append((round(now[0], 2), path))
        now[0] += 0.5              # the fetch burns half a second
        return {"target": "t", "playing": True}

    ui.api_get = slow_get
    now[0] += 0.4
    app._poll_once()               # fetch #2, completes at +0.5
    n_after = len(status_calls())
    ui.api_get = fake_get
    now[0] += 0.2                  # only 0.2 since COMPLETION
    app._poll_once()
    assert len(status_calls()) == n_after, \
        "back-to-back fetch after a slow status — completion stamp broken"
    now[0] += 0.2                  # 0.4 since completion
    app._poll_once()
    assert len(status_calls()) == n_after + 1
    print("2+3. burst at 0.3s, self-pacing from completion OK")

    # 4. window expiry: back to the 1s idle cadence
    app.poll_burst_until = now[0] - 1
    base = len(status_calls())
    for _ in range(4):             # 0.8s of ticks — under STATUS_POLL_S
        now[0] += 0.2
        app._poll_once()
    assert len(status_calls()) == base, "idle cadence must be 1s again"
    now[0] += 0.4                  # 1.2s since last fetch
    app._poll_once()
    assert len(status_calls()) == base + 1
    print("4. expired window falls back to the 1s cadence OK")
finally:
    ui.api_get, ui.time.monotonic = real_get, real_mono

# 5. a tile tap opens the burst too (_enter_now)
app2 = ui.App(FakeDisplay(), FakeInputs())
t = time.monotonic()
app2._enter_now()
assert app2.poll_burst_until > t + 4
assert app2.catch_up_until > t + 4   # tap keeps BOTH (blank-status case)
print("5. tile tap opens burst and keeps catch-up repaints OK")

print("\nPOLL BURST OK — fetch overlaps the command, persistence "
      "catches the truth, idle stays 1s.")
