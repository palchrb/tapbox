#!/usr/bin/env python3
"""Every $("...") in the PWA uses a real CSS selector (# or .).

$ is querySelector, which needs a selector — $("#storytel-form"), never
$("storytel-form"). A bare id silently returns null, the guard around it
is false, and the event handler never attaches — the form then does a
native submit, the page reloads, and the tab jumps back to the player
(field 2026-08-15: the Storytel login "did nothing"). The PWA test
harness's fake document returns a truthy element for ANY string, so it
cannot catch this; a source check can. Cheap, and it pins the whole
class."""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "pi", "web", "app.js")
src = open(APP, encoding="utf-8").read()

# $("...") with a string literal that does not begin with # or . is a
# bad selector (a bare tag like "nav button" would be valid, but the app
# only ever selects by id/class through $, and a bare word is far more
# likely the missing-# bug). Flag anything that is a plain identifier.
bad = []
for m in re.finditer(r'\$\(\s*"([^"]*)"', src):
    sel = m.group(1)
    if not sel:
        continue
    if sel[0] in "#.":
        continue
    # allow genuine tag/compound selectors (contain a space or []),
    # forbid a lone identifier that looks like a forgotten '#'
    if re.fullmatch(r"[A-Za-z][\w-]*", sel):
        line = src.count("\n", 0, m.start()) + 1
        bad.append((line, sel))

assert not bad, "bare-id selectors (missing '#'?): " + \
    ", ".join(f"line {n}: $(\"{s}\")" for n, s in bad)
print(f"1. all {src.count(chr(36) + chr(40))} $( calls use a real "
      "selector OK")

# 2. The storytel picker's dedup must span the WHOLE library, not the
#    chosen section: entry ids are sha1(target), globally unique, and a
#    still-downloading series renders checked+enabled (deliberate — a
#    stalled download stays kickable) so it rides along in every save.
#    Section-scoped dedup let it into a second section and the server
#    400'd the whole save, blocking the NEW series too (field 2026-08-16:
#    "duplicate entry id" until Kokosbananas finished downloading).
fn = src[src.index("async function addCheckedStorytel"):]
fn = fn[:fn.index("\n}")]
assert "for (const s of lib.sections)" in fn and "have.add(e.target)" in fn, \
    "the dedup set must be built from EVERY section's entries"
assert fn.index("have.add(e.target)") < fn.index("sec.entries.push"), \
    "…and built before anything is pushed"
assert "sec.entries.some" not in fn, \
    "section-scoped dedup is the bug — it must not come back"
print("2. the storytel picker dedups against the whole library OK")

# 3. backup.html carries its OWN inline script with the same $() helper,
#    so it is exposed to exactly the bug above — and being a separate page
#    it would not be covered by the app.js scan. Check it too, and check
#    every id it selects actually exists in its own markup (a typo'd id
#    silently yields null and the handler never attaches, which is how the
#    Storytel login "did nothing" in the field).
BK = os.path.join(REPO, "pi", "web", "backup.html")
bk = open(BK, encoding="utf-8").read()
bad_bk = [m.group(1) for m in re.finditer(r'\$\(\s*"([^"]*)"', bk)
          if re.fullmatch(r"[A-Za-z][\w-]*", m.group(1))]
assert not bad_bk, f"backup.html has bare-id selectors: {bad_bk}"

ids = set(re.findall(r'\bid="([^"]+)"', bk))
missing = sorted({m.group(1) for m in re.finditer(r'\$\(\s*"#([\w-]+)"', bk)}
                 - ids)
assert not missing, \
    f"backup.html selects ids that its markup never defines: {missing}"
# and the page must reach the box only through the token-gated endpoints
for route in ("/backup/status", "/backup/snapshots", "/backup/configure",
              "/backup/now", "/backup/restore"):
    assert route in bk, f"backup.html never calls {route}"
print("3. backup.html's selectors resolve and it calls the real routes OK")

# 4. Opening the page must NOT spawn restic. loadSnapshots() costs ~120MB of
#    Go plus a network round trip on the box, and it used to run on every
#    page open — including mid-playback — just to fill a dropdown in the
#    danger card nobody came to use. Health comes from /backup/status, which
#    reads two small JSON files. The button press is the consent.
tail = bk[bk.index("loadStatus();", bk.index("</section>")):]
assert "loadSnapshots();" not in tail.split("$(\"#bk-list\")")[0], \
    "backup.html must not call loadSnapshots() at page load"
assert "#bk-list" in bk, "the snapshot list needs an explicit button"

# 5. The repo password must be confirmed LOCALLY before it becomes the only
#    key to the backup — it is never fetched back from the box, so this is
#    the one moment the owner can catch a typo or be told to save it.
form = bk[bk.index("#bk-remote-form"):]
form = form[:form.index("/backup/configure")]
assert "confirm(" in form and "bk-pass" in form, \
    "the password must be confirmed locally before setup is submitted"
assert "/backup/recovery" not in bk and "recovery-note" not in bk, \
    "the box must never serve a stored secret back over the LAN"
print("4. the page spawns no restic on load, and confirms the password OK")

print("\nPWA SELECTORS OK — no $(\"id\") that should have been $(\"#id\"), "
      "and the storytel picker cannot 400 itself on a half-downloaded "
      "series.")
