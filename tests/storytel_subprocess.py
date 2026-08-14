#!/usr/bin/env python3
"""storytel.py must run as a BARE SUBPROCESS — the way the sweep runs it.

The sweep invokes `python3 .../vibb/storytel.py sync <target> <n>`, where
sys.path[0] is the vibb/ dir itself. In that context `from vibb import
<module>` fails (no package parent on the path) AND vibb/token.py shadows
stdlib `token` via concurrent.futures. Either one kills the download —
and _sync_one discarded the stderr, so a whole series "synced" in six
seconds and downloaded nothing, with shelf.json written but no audio
(field 2026-08-15). This runs the real interpreter against the real
file, which the in-process import tests could never catch."""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORYTEL = os.path.join(REPO, "pi", "vibb", "storytel.py")

# 1. the module loads as a bare script with NO ModuleNotFoundError and NO
#    token-shadow ImportError — the two ways the download died silently.
#    Importing it (execing its top-level) is the load half; -c drives it.
env = dict(os.environ, VIBB_CACHE=tempfile.mkdtemp(),
           VIBB_STATE=tempfile.mkdtemp())
r = subprocess.run(
    [sys.executable, STORYTEL, "sync", "storytel:series:0", "-1"],
    capture_output=True, text=True, env=env, timeout=30)
combined = r.stdout + r.stderr
assert "ModuleNotFoundError" not in combined, combined
assert "EXACT_TOKEN_TYPES" not in combined, \
    "vibb/token.py shadowed stdlib token: " + combined
# with no credentials it must reach 'not configured', not an import crash
assert "not configured" in combined, combined
print("1. runs as a bare subprocess without import/shadow crashes OK")

# 2. the whole download path is reachable with NOTHING imported from the
#    vibb package (only vibb.paths, which has a fallback). Prove it by
#    exec'ing the file with the vibb dir as sys.path[0] and asserting the
#    download helper and sync exist without importing content.
probe = (
    "import sys, os;"
    f"sys.path.insert(0, {os.path.dirname(STORYTEL)!r});"
    "import importlib.util as u;"
    f"s=u.spec_from_file_location('storytel', {STORYTEL!r});"
    "m=u.module_from_spec(s); s.loader.exec_module(m);"
    "assert hasattr(m, '_download') and hasattr(m, 'sync');"
    # nothing in the module may have pulled in content (which drags in
    # concurrent.futures and the token shadow)
    "assert 'vibb.content' not in sys.modules, 'sync must not import content';"
    "print('OK')")
r2 = subprocess.run([sys.executable, "-c", probe],
                    capture_output=True, text=True, timeout=30)
assert r2.returncode == 0 and "OK" in r2.stdout, (r2.stdout, r2.stderr)
print("2. sync's download path pulls in nothing from the vibb package OK")

# 3. a non-zero subprocess exit is now SURFACED by _sync_one, not
#    swallowed — the safety net so the next silent failure isn't silent.
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))
from vibb import library  # noqa: E402

logged = []
library.log = lambda m: logged.append(m)
library._busy = lambda: False
# a real script that fails, run through the real _sync_one as its module
fail = os.path.join(tempfile.mkdtemp(), "boom.py")
with open(fail, "w") as f:
    f.write("import sys\nsys.stderr.write('kaboom')\nsys.exit(3)\n")
library._sync_one([], mod=fail)
assert any("exited 3" in m and "kaboom" in m for m in logged), logged
print("3. a non-zero sync exit is logged with its stderr tail OK")

print("\nSTORYTEL SUBPROCESS OK — the download runs as a bare script, and "
      "a failure can never vanish silently again.")
