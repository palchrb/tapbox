#!/usr/bin/env python3
"""Pin the Extras containment (SECURITY.md 'Extras' section).

Handing root to a dropped script is the maximal action, so its reach
is structural: no HTTP route may ever exist for it (not even token-
authed — a linked phone must not be able to seize the box remotely),
the media-upload whitelist must never learn an executable extension,
the drop-in dir must live outside the upload roots, and install.sh
re-runs must never touch the dir's content. Source-level pins: they
break the moment someone wires a route or widens the whitelist."""
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_LIBRARY"] = os.path.join(TMP, "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

SRC = open(os.path.join(REPO, "pi", "daemon.py")).read()

# 1. no extras route in the API — nothing under /extras, no exec route
assert '"/extras' not in SRC and "'/extras" not in SRC, \
    "the API must never grow an extras route (SECURITY.md)"
assert "/system/exec" not in SRC
print("1. no /extras or exec route in the API OK")

# 2. the upload whitelist stays data-only: no executable/script types
import daemon  # noqa: E402
BANNED = {".sh", ".bash", ".py", ".pl", ".rb", ".bin", ".run", ".elf",
          ".service", ".desktop"}
assert not (set(daemon.MEDIA_EXTS) & BANNED), daemon.MEDIA_EXTS
# ...and the sanitizer keeps every name inside the media dir
for evil in ("../x.mp3", "..\\x.mp3", "/etc/tapbox/extras/x.mp3",
             "a/../../x.mp3", "x.sh", "x.mp3.sh"):
    safe = daemon._media_safe_name(evil)
    assert "/" not in safe and "\\" not in safe and ".." not in safe, evil
    assert not safe.endswith(".sh"), evil
print("2. upload whitelist data-only, sanitizer basename-bound OK")

# 3. the extras dir default is outside the media/upload roots
import ui  # noqa: E402
extras_default = "/etc/tapbox/extras"
from tapbox import paths  # noqa: E402
for root in (getattr(paths, "MEDIA_DIR", ""), getattr(paths, "CACHE_DIR", "")):
    if root:
        assert not extras_default.startswith(root.rstrip("/") + "/"), \
            f"extras dir must not live under {root}"
print("3. extras dir outside media/cache roots OK")

# 4. install.sh: creates the dir, never writes into or removes it
sh = open(os.path.join(REPO, "pi", "install.sh")).read()
assert re.search(r"install -d -m 755 /etc/tapbox/extras", sh), \
    "install.sh must create the drop-in dir"
assert not re.search(r"rm\s+(-\w+\s+)*['\"]?/etc/tapbox/extras", sh), \
    "install.sh must never delete the extras dir"
assert not re.search(r"(cp|install)\s[^\n]*extras/", sh), \
    "install.sh must never place files INSIDE the extras dir"
print("4. install.sh creates but never touches the dir's content OK")

# 5. the UI's gate: wrong-owner or writable files never listed (the
#    deep test lives in ui_extras.py; here just pin the constants so a
#    refactor can't silently relocate the dir into an upload root)
assert ui.EXTRAS_DIR == os.environ.get("TAPBOX_EXTRAS", extras_default)
assert ui.EXTRA_WRAPPER == "/usr/local/bin/tapbox-extra"
print("5. UI constants pinned OK")

print("\nall extras_scope_guard checks passed")
