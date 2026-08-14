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

print("\nPWA SELECTORS OK — no $(\"id\") that should have been $(\"#id\").")
