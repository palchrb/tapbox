#!/usr/bin/env python3
"""Gate the v0.1.0 next-track art prewarm: /status exposes the upcoming
track's cover (spotify.next_artwork, from the fork's metadata cache) and
the UI poller fetches it into the art cache BEFORE the kid presses next —
so the skip paints its cover instantly instead of racing the settle load
over the shared radio ("tekst men ikke art", field 2026-07-23)."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["TAPBOX_RUN"] = tempfile.mkdtemp()
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402

app = ui.App.__new__(ui.App)
now = time.monotonic()
app.view = "now"
app.user_touched = False
app.last_status = now   # fresh — the poll skips the /status fetch itself
app.last_system = now   # fresh — skips the /system branch
app.load_library = lambda: None
app.status = {"spotify": {"next_artwork": "https://i.scdn.co/next-cover"}}

warmed = []
app.artwork_async = lambda ref, size=110, square=False: \
    warmed.append((ref, size, square))

# 1. a poll with next_artwork present prewarms the now-card size (128 sq)
app._poll_once()
assert warmed == [("https://i.scdn.co/next-cover", 128, True)], warmed
print("1. next_artwork prewarmed at the now-card size OK")

# 2. no next_artwork -> no fetch attempt at all
warmed.clear()
app.status = {"spotify": {"next_artwork": None}}
app._poll_once()
assert warmed == [], warmed
app.status = {}
app._poll_once()
assert warmed == [], warmed
print("2. absent next_artwork: no prewarm attempt OK")

print("\nall ui_next_art_prewarm checks passed")
