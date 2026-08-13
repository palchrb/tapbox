#!/usr/bin/env python3
"""Gate the on-box Bluetooth menu, including inbound 'Pair from car'.

Every pairing direction must be reachable from the box's four buttons —
a car stereo insists on STARTING the pairing itself, so reaching out
(Pair nearest / Scan) can never work for it; the box has to become
discoverable instead. That used to be PWA-only.

Also pins the dispatch: like the settings menu, this list is
label-keyed, because its action rows have now grown twice and an
index-keyed dispatch misfires silently the next time (the settings menu
version of this bug once made 'Restart' power the box off).

And: these all hit PRIVILEGED endpoints since the API gate landed, so
the screen must authenticate as the box — this test would catch the day
the UI stops sending the token."""
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "api-token")
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402

app = ui.App.__new__(ui.App)
app.view = "bt"
app.sel = 0
app.stack = []
app.dirty = False
app.bt = {"configured": "AA:BB", "devices": [
    {"mac": "AA:BB", "name": "JBL JR310BT", "connected": True},
    {"mac": "CC:DD", "name": "Bilstereo", "connected": False}]}
app.bt_found = []
app.draw_message = lambda *a, **k: None
app.push = lambda v: setattr(app, "view", v)

POSTS, GETS = [], []
ui.api_post = lambda p, b=None, **k: (POSTS.append((p, b)), {"ok": True})[1]
ui.api_get = lambda p, **k: (GETS.append(p), app.bt)[1]
ui.time = types.SimpleNamespace(sleep=lambda s: None,
                                monotonic=lambda: 0.0)

labels = [r[0] for r in app.current_items()]
assert labels[:3] == ["Pair nearest", "Scan for new", "Pair from car"], labels
# the known devices follow the action rows
assert labels[3:] == ["JBL JR310BT", "Bilstereo"], labels
print("1. bt menu lists all three pairing actions, then the devices OK")

# 2. outbound: pair nearest
POSTS.clear()
app.sel = labels.index("Pair nearest")
app.select_bt()
assert POSTS and POSTS[0][0] == "/bt/pair", POSTS
print("2. 'Pair nearest' pairs outbound OK")

# 3. scan opens the found-devices view
POSTS.clear()
app.sel = labels.index("Scan for new")
app.select_bt()
assert POSTS and POSTS[0][0] == "/bt/scan", POSTS
assert app.view == "btscan", app.view
app.view = "bt"
print("3. 'Scan for new' scans and opens the list OK")

# 4. THE NEW ONE: inbound pairing for a car stereo. It must ask for a
#    discoverable WINDOW and give the API time to sit through it —
#    a timeout shorter than the window would abort the very wait it
#    asked for.
POSTS.clear()
app.sel = labels.index("Pair from car")
app.select_bt()
assert POSTS and POSTS[0][0] == "/bt/visible", POSTS
secs = POSTS[0][1]["secs"]
assert 60 <= secs <= 300, f"a usable discoverable window: {secs}"
print(f"4. 'Pair from car' makes the box visible for {secs}s OK")

# 5. picking a known device connects to THAT device — the index maths
#    below the action rows must stay correct as rows are added
POSTS.clear()
app.sel = labels.index("Bilstereo")
app.select_bt()
assert POSTS == [("/bt/connect", {"mac": "CC:DD"})], \
    f"must connect the device the cursor is on: {POSTS}"
POSTS.clear()
app.sel = labels.index("JBL JR310BT")
app.select_bt()
assert POSTS == [("/bt/connect", {"mac": "AA:BB"})], POSTS
print("5. device rows connect the right device (offset survives) OK")

# 6. the screen authenticates as the box: boxapi must attach the token,
#    or every one of the above becomes a 401 on a real box
from vibb import boxapi, token  # noqa: E402

token.ensure()
sent = {}


class _FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


def _fake_urlopen(req, timeout=None):
    sent.update(req.headers)
    return _FakeResp()


boxapi.urllib.request.urlopen = _fake_urlopen
boxapi.post("/bt/pair", {})
hdr = {k.lower(): v for k, v in sent.items()}
assert hdr.get("X-vibb-token".lower()) == token.read(), \
    f"the screen must authenticate to its own API: {sent}"
print("6. the UI's API calls carry the box token OK")

print("\nall ui_bt_menu checks passed")
