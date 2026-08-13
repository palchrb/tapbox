#!/usr/bin/env python3
"""Gate the on-demand wifi reconnect (the offline-Spotify popup's X, and
pressing play on a Spotify tile with no net): unblock the radio, wait for
a known network, and on the box screen offer it on X instead of a
dead-end 'can't play'. Better than waiting out the auto-off prober's
10-minute timer when someone is standing there wanting the net."""
import json
import os
import sys
import tempfile
import threading
import time
import types
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "api-token")
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ.setdefault("VIBB_CACHE", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import netmgmt  # noqa: E402


# --- 1. netmgmt.wifi_reconnect: unblock + wait for a known network ----------

netmgmt.time = types.SimpleNamespace(monotonic=time.monotonic,
                                     time=time.time, sleep=lambda s: None)
RF, NET = [], {"ssid": None}
netmgmt._rfkill = lambda on: (RF.append(on),
                              NET.__setitem__("ssid", "homenet" if on else None))
netmgmt.wifi_state = lambda: (True, NET["ssid"], "1.2.3.4" if NET["ssid"] else None)

r = netmgmt.wifi_reconnect(window_s=30)
assert r == {"ok": True, "ssid": "homenet"}, r
assert RF == [True], f"must unblock the radio: {RF}"
print("1. wifi_reconnect unblocks + reports the joined network OK")

# nothing in range -> ok False, no exception
NET["ssid"] = None
netmgmt._rfkill = lambda on: RF.append(on)  # unblock, but nothing joins
netmgmt.wifi_state = lambda: (True, None, None)
r = netmgmt.wifi_reconnect(window_s=1)
assert r == {"ok": False, "ssid": None}, r
print("2. wifi_reconnect reports failure when nothing joins OK")

# busy (lock held) -> None (409 upstream)
netmgmt.WIFI_LOCK.acquire()
try:
    assert netmgmt.wifi_reconnect(1) is None
finally:
    netmgmt.WIFI_LOCK.release()
print("3. busy wifi -> None (409) OK")


# --- 2. daemon POST /wifi/reconnect: quiesce, clear offline, unpark ---------

os.environ["VIBB_BT_BACKEND"] = "cli"
import daemon  # noqa: E402

QUIESCED, RESUMED, STARTED = [], [], []
daemon._bt_quiesce = lambda: QUIESCED.append(1) or True
daemon._bt_resume = lambda r: RESUMED.append(r)
daemon.subprocess = types.SimpleNamespace(
    run=lambda *a, **k: STARTED.append(a[0]),
    DEVNULL=None, TimeoutExpired=Exception)
RECON = {"ok": True, "ssid": "homenet"}
daemon.wifi_reconnect = lambda secs=30: RECON
daemon._SPOT_OFFLINE[0] = True

srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def _box_token():
    """Privileged endpoints need the box token since the API gate landed.
    ensure() returns the daemon's existing one, or creates it when the
    daemon runs in-process here and never went through main()."""
    from vibb import token
    return token.ensure()

def post(path, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "X-Vibb-Token": _box_token()}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read())

code, r = post("/wifi/reconnect", {"secs": 30})
assert code == 200 and r["ok"], (code, r)
assert QUIESCED and RESUMED, "must quiesce/resume A2DP around the scan"
assert daemon._SPOT_OFFLINE[0] is False, "success must clear the offline flag"
deadline = time.monotonic() + 3
while time.monotonic() < deadline and not STARTED:
    time.sleep(0.02)
assert any("go-librespot" in a for a in STARTED), \
    f"success must unpark go-librespot: {STARTED}"
print("4. /wifi/reconnect quiesces, clears offline, unparks go-librespot OK")

# failure: offline flag stays, go-librespot not started
STARTED.clear()
daemon._SPOT_OFFLINE[0] = True
RECON = {"ok": False, "ssid": None}
code, r = post("/wifi/reconnect", {"secs": 5})
assert code == 200 and not r["ok"], (code, r)
assert daemon._SPOT_OFFLINE[0] is True, "failed reconnect must stay offline"
time.sleep(0.2)
assert not STARTED, "no unpark on a failed reconnect"
print("5. failed reconnect leaves offline flag + go-librespot alone OK")

# /wifi/scan sweeps all 13 channels off-frequency — as A2DP-hostile as
# BT discovery, so it must take the same quiesce, but ONLY on the bt
# output (a scan can't hurt the built-in speaker, and stopping local
# playback for it would be an audible interruption for nothing)
daemon.wifi_scan = lambda: [{"ssid": "homenet"}]
with open(daemon.OUT_FILE, "w") as f:
    json.dump({"output": "bt", "pcm": "vibb_bt"}, f)
QUIESCED.clear()
RESUMED.clear()
code, r = post("/wifi/scan", {})
assert code == 200 and QUIESCED and RESUMED, (code, QUIESCED, RESUMED)
print("5b. /wifi/scan on the bt output quiesces A2DP around the sweep OK")

with open(daemon.OUT_FILE, "w") as f:
    json.dump({"output": "local", "pcm": "vibb_local"}, f)
QUIESCED.clear()
RESUMED.clear()
code, r = post("/wifi/scan", {})
assert code == 200 and not QUIESCED, (code, QUIESCED)
print("5c. /wifi/scan on the built-in output never touches playback OK")


# --- 3. UI wiring: X on the offline-Spotify now-view reconnects -------------

os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
import ui  # noqa: E402

POSTS, VOL = [], []
ui.api_post = lambda path, body=None, timeout=15: POSTS.append((path, body)) \
    or {"ok": True}
ui.api_get = lambda path, timeout=10: {}

app = object.__new__(ui.App)
app.status = {"spotify_offline": True, "source": "spotify"}
app.system = {}
app.wifi_connecting_until = 0.0
app.bt_connecting_until = 0.0
app.vol_mode_until = 0.0
app.last_status = 1e18
app.last_system = 0.0
app._volume_mode = lambda **k: VOL.append(k)

app.handle_now("x")
deadline = time.monotonic() + 3
while time.monotonic() < deadline and not POSTS:
    time.sleep(0.02)
assert POSTS == [("/wifi/reconnect", {"secs": 30})], POSTS
assert not VOL, "X on the offline popup must reconnect, not open volume"
print("6. X on the offline-Spotify now-view reconnects Wi-Fi OK")

# online (or non-spotify) -> X is the volume card as before
POSTS.clear()
app.wifi_connecting_until = 0.0
app.status = {}
app.handle_now("x")
time.sleep(0.2)
assert not POSTS and len(VOL) == 1, (POSTS, VOL)
print("7. X without the offline popup is the volume card OK")

print("WIFI RECONNECT OK — on-demand net, X where it matters.")
