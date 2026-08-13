#!/usr/bin/env python3
"""Verify the Spotify account-lock logic: zeroconf open/close, auto-lock
transition, and the switch-account reopen. No bus, no systemctl (the
module swallows the missing-binary error), runs anywhere."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp()
CONF = os.path.join(tmp, "config.yml")
STATE = os.path.join(tmp, "state.json")
open(CONF, "w").write(
    "device_name: zero2\nzeroconf_enabled: true\n"
    "credentials:\n  type: zeroconf\n")
os.environ["VIBB_GO_CONFIG"] = CONF

sys.path.insert(0, os.path.join(REPO, "pi"))
from vibb import spotify as s  # noqa: E402


def logged_in(user):
    if user is None:
        try:
            os.remove(STATE)
        except OSError:
            pass
    else:
        json.dump({"credentials": {"username": user}}, open(STATE, "w"))


# fresh install: door open, nobody on
assert s.zeroconf_open() is True
assert s.logged_in_user() is None
assert s.lock() is False, "must not lock before login"
print("1. open + no user -> lock is a no-op OK")

# account logs in -> auto-lock closes the door exactly once
logged_in("31pjuhnzbkf6loikcskprk2gvmtq")
assert s.logged_in_user() == "31pjuhnzbkf6loikcskprk2gvmtq"
assert s.lock() is True, "should lock on first login"
assert s.zeroconf_open() is False, "door must be closed after lock"
assert s.lock() is False, "second lock is a no-op (already closed)"
print("2. login -> lock once, then idempotent OK")

# locked config still carries the account (survives reboot)
assert s.logged_in_user() == "31pjuhnzbkf6loikcskprk2gvmtq"
assert "zeroconf_enabled: false" in open(CONF).read()
assert "credentials:" in open(CONF).read(), "rest of config preserved"
print("3. locked config keeps creds + rest intact OK")

# switch account: reopen the door, forget the login
r = s.logout()
assert r["ok"] and r.get("open") is True, r
assert s.zeroconf_open() is True, "door reopened for a new login"
assert s.logged_in_user() is None, "old login forgotten"
assert "state.json" in r["removed"]
print("4. switch account reopens + forgets OK")

# new account claims it -> auto-lock closes again
logged_in("someoneelse")
assert s.lock() is True
assert s.zeroconf_open() is False
print("5. new account -> re-locks OK")

print("SPOTIFY LOCK OK — one-account lock + reversible switch intact.")
