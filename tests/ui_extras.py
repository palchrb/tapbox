#!/usr/bin/env python3
"""Gate the Extras owner-script menu (docs/extras.md).

The scan IS the security boundary on the screen side: only regular
executables owned by our uid and not group/world-writable qualify —
anything a kid or an unprivileged process could plant or edit must be
invisible. The chord must be a no-op on a stock box, and the launch
must go through the transient-unit + ExecStopPost recipe (the return
guarantee QA blocked on)."""
import os
import stat
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
EXTRAS = os.path.join(TMP, "extras")
os.environ["TAPBOX_EXTRAS"] = EXTRAS
os.environ["TAPBOX_RUN"] = TMP
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402


def app():
    a = ui.App.__new__(ui.App)
    a.view = "home"
    a.sel = 0
    a.stack = []
    a.dirty = False
    a.user_touched = False
    a.draw_message = lambda *k, **kw: None
    return a


# 1. stock box: no dir at all -> the chord event is a NO-OP
a = app()
a.handle("extras")
assert a.view == "home", "chord must do nothing without extras"
os.makedirs(EXTRAS)
a.handle("extras")
assert a.view == "home", "empty dir: still a no-op"
print("1. stock box: X+Y chord is a no-op OK")


def drop(name, body="#!/bin/sh\ntrue\n", mode=0o755):
    p = os.path.join(EXTRAS, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, mode)
    return p


# 2. the scan's security filter: non-executables, group/world-writable
#    files and wrong-owner files are INVISIBLE
drop("retropie.sh", "#!/bin/sh\n# tapbox-name: RetroPie\ntrue\n")
drop("not-exec.sh", mode=0o644)
drop("loose.sh", mode=0o777)          # world-writable = plantable
drop("group-write.sh", mode=0o775)    # group-writable too
sub = os.path.join(EXTRAS, "subdir")
os.makedirs(sub)                       # directories are skipped
found = ui.App.extras(a)
assert [e["name"] for e in found] == ["RetroPie"], found
print("2. scan filter: only clean root-owned executables show OK")

# 3. names: header comment wins, filename fallback title-cases
drop("night_light-mode.sh")
names = [e["name"] for e in ui.App.extras(a)]
assert names == ["Night Light Mode", "RetroPie"], names
print("3. tapbox-name header + filename fallback OK")

# 4. the chord now opens the menu, rows match the scan
a.handle("extras")
assert a.view == "extras"
rows = a.current_items()
assert [r[0] for r in rows] == ["Night Light Mode", "RetroPie"], rows
a.handle("extras")  # chord again while open: no double-push
assert a.stack[-1][0] != "extras"
print("4. chord opens the menu, no double-push OK")

# 5. launch: A-confirm required, and the systemd-run recipe carries the
#    transient unit + ExecStopPost restore (the return guarantee)
LAUNCHED = []
ui.subprocess = type("S", (), {"Popen": staticmethod(
    lambda argv, **k: LAUNCHED.append(argv))})()
a.sel = 1  # RetroPie
a.confirm = lambda timeout=5: False
a.select()
assert LAUNCHED == [], "B/timeout must cancel the launch"
a.confirm = lambda timeout=5: True
a.select()
assert len(LAUNCHED) == 1, LAUNCHED
argv = LAUNCHED[0]
assert argv[0] == "systemd-run" and "--collect" in argv
assert "--unit=tapbox-extra" in argv
assert "--property=Restart=no" in argv, \
    "a crash-looping extra must not respawn"
assert any(p.startswith("--property=ExecStopPost=") and "--restore" in p
           for p in argv), "ExecStopPost restore is the return guarantee"
assert argv[-2:] == ["--run", os.path.join(EXTRAS, "retropie.sh")]
print("5. launch: confirm-gated, transient unit + ExecStopPost OK")

# 6. select on the extras view with a stale sel is harmless
a.sel = 99
a.select()
assert len(LAUNCHED) == 1
print("6. stale selection: no launch OK")

# 6b. THE BLACK-MENU REGRESSION (field 2026-07-29): the extras view
#     must actually RENDER — it existed and handled input (A asked
#     'Start RetroPie?') but had no render branch, so the menu was a
#     black screen. Pin: render() on view=extras draws a non-empty
#     frame containing a highlighted list row.
class FakeDisplay:
    last = None

    def show(self, img):
        FakeDisplay.last = img


a.view = "extras"
a.sel = 0
a.display = FakeDisplay()
a.status = {}
a.system = {}
a.marquee_active = False
a.render()
frame = FakeDisplay.last
assert frame is not None, "render must show a frame"
colors = {frame.getpixel((x, y)) for x in range(0, ui.W, 8)
          for y in range(0, ui.H, 8)}
assert len(colors) > 2, "extras menu must not render as a blank screen"
print("6b. extras view renders a real list (black-menu regression) OK")

# 7. the startup message contract: fresh note is returned once and the
#    file deleted; a stale note is deleted UNSHOWN (must never greet
#    tomorrow's boot)
with open(ui.EXTRA_MSG_FILE, "w") as f:
    f.write("RetroPie: no TV found — connect HDMI and try again\n")
msg = ui.consume_extra_msg()
assert msg == "RetroPie: no TV found — connect HDMI and try again", msg
assert not os.path.exists(ui.EXTRA_MSG_FILE), "consuming must delete"
assert ui.consume_extra_msg() is None, "second read: nothing"
with open(ui.EXTRA_MSG_FILE, "w") as f:
    f.write("old news")
old = ui.time.time() - ui.EXTRA_MSG_FRESH_S - 10
os.utime(ui.EXTRA_MSG_FILE, (old, old))
assert ui.consume_extra_msg() is None, "stale note must not show"
assert not os.path.exists(ui.EXTRA_MSG_FILE), "stale note still deleted"
print("7. extras screen-message: fresh shown once, stale swallowed OK")

print("\nall ui_extras checks passed")
