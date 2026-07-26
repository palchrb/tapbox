#!/usr/bin/env python3
"""Gate the on-box 'Link phone' screen and the settings dispatch.

Two things are under test:

1. THE DISPATCH. The settings menu used to act on the row INDEX, so
   every inserted row shifted the actions below it — one inverted flag
   from that once made "Restart" power the box off instead (field
   report). Inserting "Link phone" is exactly that hazard, so dispatch
   is label-based now and this test pins the mapping: whatever the row
   order becomes, the label decides the action.

2. THE LINK VIEW. It shows the token as a QR (the credential handoff
   that proves you're standing at the box) plus the same token in
   typeable form — and it must still provision a phone when the qrcode
   lib is missing, which is the difference between "degraded" and "a
   box nobody can ever link"."""
import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_RUN"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_TOKEN_FILE"] = os.path.join(TMP, "api-token")
os.environ.setdefault("TAPBOX_UI_PNG", "/dev/null")
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402
from tapbox import token  # noqa: E402

TOKEN = token.ensure()

app = ui.App.__new__(ui.App)
app.view = "settings"
app.sel = 0
app.stack = []
app.dirty = False
app.settings = {"screen_timeout_s": 30, "screen_brightness": 100,
                "volume_cap": 80, "idle_shutdown_min": 30, "simple_nav": 0}
app.system = {"wifi": {"enabled": True, "hotspot": False, "ip": "10.0.0.21"}}
app._nav_mode = lambda: 0

rows = [r[0] for r in app.current_items()]
assert "Link phone" in rows, rows
print(f"0. settings menu has {len(rows)} rows incl. 'Link phone' OK")

# 1. THE HAZARD: every row must act on ITS OWN label, whatever the
#    index. Walk every row and record what each one did.
acted = []
app.draw_message = lambda *a, **k: None
app.confirm = lambda: True
app.push = lambda v: acted.append(("push", v))
app.display = types.SimpleNamespace(set_brightness=lambda p: None)
ui.api_put = lambda p, b, **k: (acted.append(("put", p, b)), app.settings)[1]
ui.api_post = lambda p, b=None, **k: (acted.append(("post", p, b)), {})[1]
ui.api_get = lambda p, **k: (acted.append(("get", p)), {})[1]

for i, label in enumerate(rows):
    acted.clear()
    app.sel = i
    app.select_setting()
    if label == "Shut down":
        assert acted == [("post", "/system/shutdown", {"restart": False})], \
            f"'Shut down' must NOT restart: {acted}"
    elif label == "Restart":
        assert acted == [("post", "/system/shutdown", {"restart": True})], \
            f"'Restart' must NOT power off: {acted}"
    elif label == "Link phone":
        assert acted == [("push", "link")], f"Link phone -> link: {acted}"
    elif label == "Storage":
        assert acted == [("push", "storage")], acted
    elif label == "Bluetooth":
        assert ("get", "/bt") in acted and ("push", "bt") in acted, acted
    elif label == "Wi-Fi":
        assert acted and acted[0][1] == "/system/wifi", acted
    elif label == "Setup hotspot":
        assert acted and acted[0][1] == "/wifi/hotspot", acted
    else:  # the settings cycles
        assert acted and acted[0][0] == "put", f"{label}: {acted}"
print("1. every settings row acts on its own label (no index drift) OK")

# 1b. THE ORIGIN RULE: the QR must target the box's stable <name>.local,
#     never its IP. The browser keeps the token per ORIGIN, so an
#     IP-based link is lost the moment DHCP moves the box or it comes up
#     as its own hotspot — the parent would have to re-scan every time.
import types as _t  # noqa: E402

from tapbox import netmgmt  # noqa: E402

AVAHI = os.path.join(TMP, "avahi.conf")
with open(AVAHI, "w") as f:
    f.write("[server]\nhost-name=stuebox\n")
netmgmt.AVAHI_CONF = AVAHI
assert netmgmt.mdns_host() == "stuebox.local", netmgmt.mdns_host()

captured = {}


class _FakeQR:
    def __init__(self, **k):
        pass

    def add_data(self, d):
        captured["url"] = d

    def make(self, **k):
        pass

    def get_matrix(self):
        return [[0] * 35] * 35

    def make_image(self, **k):
        from PIL import Image as _I
        return _I.new("RGB", (35, 35), (255, 255, 255))


fake_qr = _t.SimpleNamespace(QRCode=_FakeQR,
                             constants=_t.SimpleNamespace(ERROR_CORRECT_M=0))
sys.modules["qrcode"] = fake_qr
from PIL import Image as _Img, ImageDraw as _Draw  # noqa: E402

app.system = {"wifi": {"ip": "10.0.0.21"}}
_i = _Img.new("RGB", (ui.W, ui.H))
app.render_link(_Draw.Draw(_i), _i)
assert captured["url"].startswith("http://stuebox.local:3679/#t="), \
    f"the QR must use the stable .local origin: {captured['url']}"
assert "10.0.0.21" not in captured["url"], \
    f"the QR must NOT pin the IP (origin would change on DHCP): {captured['url']}"
assert captured["url"].endswith(token.read()), captured["url"]
print("1b. QR targets <name>.local (stable origin), token in the fragment OK")
del sys.modules["qrcode"]

# 2. the link view renders WITH qrcode present
from PIL import Image, ImageDraw  # noqa: E402

img = Image.new("RGB", (ui.W, ui.H))
app.render_link(ImageDraw.Draw(img), img)
assert img.getpixel((5, 5)) == (255, 255, 255), \
    "the link screen must be white for camera contrast"
print("2. link view renders (QR on white) OK")

# 3. THE FALLBACK THAT MATTERS: no qrcode lib (an install where pip
#    failed) must still show the typeable token — not a dead screen that
#    can never link a phone
real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") \
    else __builtins__["__import__"]


def no_qrcode(name, *a, **k):
    if name == "qrcode":
        raise ImportError("no qrcode")
    return real_import(name, *a, **k)


try:
    if hasattr(__builtins__, "__import__"):
        __builtins__.__import__ = no_qrcode
    else:
        __builtins__["__import__"] = no_qrcode
    img2 = Image.new("RGB", (ui.W, ui.H))
    app.render_link(ImageDraw.Draw(img2), img2)
finally:
    if hasattr(__builtins__, "__import__"):
        __builtins__.__import__ = real_import
    else:
        __builtins__["__import__"] = real_import
assert img2.getpixel((5, 5)) == (255, 255, 255)
assert img2.tobytes() != Image.new("RGB", (ui.W, ui.H),
                                   (255, 255, 255)).tobytes(), \
    "without qrcode the screen must still print the token, not go blank"
print("3. no qrcode lib: the typeable token still shows (can still link) OK")

# 4. A on the link view rotates the token (every phone must re-link), and
#    it is confirmed first — the button is one press from a kid's hands
app.view = "link"
before = token.read()
app.confirm = lambda: False
app.select()
assert token.read() == before, "a cancelled confirm must NOT rotate"
app.confirm = lambda: True
app.select()
after = token.read()
assert after and after != before, "A must rotate the token"
print("4. A rotates the token, and only after a confirm OK")

# 5. a box with no token file says so instead of showing a blank screen
os.remove(token.TOKEN_FILE)
img3 = Image.new("RGB", (ui.W, ui.H))
app.render_link(ImageDraw.Draw(img3), img3)
print("5. missing token file renders an explanation, no crash OK")

# 5b. SHOWING the QR must never ROTATE. Rotating on display would
#     unlink every other phone in the house each time someone glanced at
#     this screen — the token is stable, and only the explicit A press
#     (confirmed, above) issues a new one.
token.ensure()
stable = token.read()
for _ in range(3):
    _img = Image.new("RGB", (ui.W, ui.H))
    app.render_link(ImageDraw.Draw(_img), _img)
    assert token.read() == stable, "rendering the QR must not rotate the token"
print("5b. showing the QR never rotates the token OK")

# 6. THE SCAN WINDOW: the screen must NOT blank while the QR is up —
#    it blanks after 30s of no input by default, which is exactly the
#    moment someone is fumbling their phone camera into position.
app.view = "link"
app.settings["screen_timeout_s"] = 30
app._link_since = time.monotonic()
app.last_input = time.monotonic() - 120  # long past the normal timeout
assert app.screen_should_sleep() is False, \
    "the link screen must stay lit while someone is scanning it"
print("6. link screen stays lit past the normal blank timeout OK")

# 6b. ...but bounded: it holds a SECRET, so after LINK_AWAKE_S normal
#     sleep resumes (and the loop backs out of the view, below)
app._link_since = time.monotonic() - ui.LINK_AWAKE_S - 1
assert app.screen_should_sleep() is True, \
    "past the window the link screen must sleep like any other"
print("6b. past the window it sleeps again (battery + secret on screen) OK")

# 6c. other views are unaffected — no accidental never-sleep
app.view = "settings"
assert app.screen_should_sleep() is True
app.settings["screen_timeout_s"] = 0   # 'never' still means never
app.view = "link"
app._link_since = time.monotonic()
assert app.screen_should_sleep() is False
print("6c. the exemption is scoped to the link view OK")

print("\nall ui_link_phone checks passed")
