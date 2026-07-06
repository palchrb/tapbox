"""Filesystem locations shared by every component (env-overridable)."""

import json
import os

STATE_DIR = os.environ.get("TAPBOX_STATE", "/var/lib/tapbox/state")
CACHE_DIR = os.environ.get("TAPBOX_CACHE", "/var/lib/tapbox/cache")
SETTINGS_FILE = os.environ.get("TAPBOX_SETTINGS", "/etc/tapbox/settings.json")


def read_settings():
    """The raw settings dict ({} when missing/invalid). Validation and
    defaults live in the daemon — consumers treat this as advisory."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
