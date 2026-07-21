"""Filesystem locations shared by every component (env-overridable)."""

import json
import os
import time

STATE_DIR = os.environ.get("TAPBOX_STATE", "/var/lib/tapbox/state")
CACHE_DIR = os.environ.get("TAPBOX_CACHE", "/var/lib/tapbox/cache")
# Uploaded section logos — user content, so NOT under CACHE_DIR's pruning
ART_DIR = os.environ.get("TAPBOX_ART", "/var/lib/tapbox/art")
SETTINGS_FILE = os.environ.get("TAPBOX_SETTINGS", "/etc/tapbox/settings.json")

# Advisory 'a human pressed a button' marker, mtime is the fact — same
# contract as the radio markers (tmpfs, crash-safe, best-effort). The
# UI touches it on input; tapbox-idle reads it so the box never powers
# off in a kid's hands while they browse without playing anything.
RUN_DIR = os.environ.get(
    "TAPBOX_RUN", "/run" if os.access("/run", os.W_OK) else "/tmp")
ACTIVITY_FILE = os.path.join(RUN_DIR, "tapbox-ui-activity")
_ACT_TOUCHED = [0.0]


def touch_activity():
    """Record 'someone is using the buttons right now'. Throttled so a
    burst of presses costs one tmpfs write per 10s; failures are
    swallowed — this can only ever delay an auto-shutdown, never break
    playback."""
    now = time.monotonic()
    if _ACT_TOUCHED[0] and now - _ACT_TOUCHED[0] < 10:
        return
    try:
        with open(ACTIVITY_FILE, "w"):
            pass
        _ACT_TOUCHED[0] = now
    except OSError:
        pass


def last_activity():
    """Epoch mtime of the last button press, 0.0 when never/unknown."""
    try:
        return os.path.getmtime(ACTIVITY_FILE)
    except OSError:
        return 0.0


# Shared 'go-librespot was just restarted' marker. go-librespot gets
# restarted from three places on the same BT event — bt.py's ALSA-route
# rewrite, output.py's audio_device retarget, and the daemon's dead-
# device rebuild — each for its own config change. This mtime lets a
# later restart that has NOTHING new to apply skip a redundant second
# bounce (which just re-bursts the shared 2.4GHz radio and re-flaps the
# speaker). Advisory, crash-safe, tmpfs — same contract as the radio
# markers.
GO_RESTART_FILE = os.path.join(RUN_DIR, "tapbox-go-restart")


def note_go_restart():
    """Record that go-librespot was just (re)started, from any path."""
    try:
        with open(GO_RESTART_FILE, "w"):
            pass
    except OSError:
        pass


def go_restarted_within(secs):
    """True if go-librespot was (re)started less than `secs` ago. A
    future mtime (clock jumped back) reads as 'not recent' — a harmless
    extra restart beats wrongly skipping a needed one."""
    try:
        age = time.time() - os.path.getmtime(GO_RESTART_FILE)
    except OSError:
        return False
    return 0 <= age < secs


def read_settings():
    """The raw settings dict ({} when missing/invalid). Validation and
    defaults live in the daemon — consumers treat this as advisory."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
