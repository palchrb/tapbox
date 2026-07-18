#!/usr/bin/env python3
"""Gate the PWA-adjustable Spotify bitrate: the setting must accept only
the rates go-librespot actually streams (96/160/320 kbps Ogg Vorbis — an
in-range 200 would keep it from STARTING), rewrite config.yml, and
restart the service exactly when the value really changed. 320 is 'best'
but doubles CDN airtime per track on the shared radio, so the default
stays 160 and the choice is the parent's."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["TAPBOX_STATE"] = TMP
os.environ["TAPBOX_CACHE"] = tempfile.mkdtemp()
os.environ["TAPBOX_SETTINGS"] = os.path.join(TMP, "settings.json")
GO_CONF = os.path.join(TMP, "config.yml")
os.environ["TAPBOX_GO_CONFIG"] = GO_CONF
sys.path.insert(0, os.path.join(REPO, "pi"))

from tapbox import output, sysinfo  # noqa: E402

RESTARTS = []
output.subprocess.run = lambda cmd, **k: RESTARTS.append(cmd)


def write_conf(bitrate_line="bitrate: 160  # 96 | 160 | 320"):
    with open(GO_CONF, "w") as f:
        f.write("device_name: TapBox\n" + bitrate_line
                + "\naudio_device: tapbox_bt\n")


def conf_bitrate():
    with open(GO_CONF) as f:
        for line in f:
            if line.startswith("bitrate:"):
                return line
    return None


# 1. the default is 160 (the install.sh config parity — no surprise
# restart on the first settings read)
assert sysinfo.SETTING_SPECS["spotify_bitrate"][0] == 160
assert sysinfo.load_settings()["spotify_bitrate"] == 160
print("1. default bitrate 160 matches the installed config OK")

# 2. update to 320 -> saved, config rewritten, ONE restart
write_conf()
merged = sysinfo.update_settings({"spotify_bitrate": 320})
assert merged["spotify_bitrate"] == 320
assert "320" in conf_bitrate(), conf_bitrate()
assert len(RESTARTS) == 1 and "restart" in RESTARTS[0], RESTARTS
print("2. 320 saves, rewrites config.yml, restarts go-librespot once OK")

# 3. re-saving the SAME value must not bounce the service (a settings
# page re-save mid-playback would audibly kill the music for nothing)
RESTARTS.clear()
sysinfo.update_settings({"spotify_bitrate": 320})
assert RESTARTS == [], f"unchanged bitrate must not restart: {RESTARTS}"
print("3. unchanged value: no restart, no playback hiccup OK")

# 4. in-range but invalid rates are refused (200 kbps would keep
# go-librespot from starting at all — worse than any bitrate)
for bad in (200, 128, 97):
    try:
        sysinfo.update_settings({"spotify_bitrate": bad})
        raise SystemExit(f"accepted invalid bitrate {bad}")
    except ValueError:
        pass
assert sysinfo.load_settings()["spotify_bitrate"] == 320
print("4. only 96/160/320 accepted (160-320 range is not enough) OK")

# 5. a corrupt settings file (hand-edited to 128) snaps back to the
# default on load instead of feeding go-librespot a rate it can't start
with open(os.environ["TAPBOX_SETTINGS"]) as f:
    saved = json.load(f)
saved["spotify_bitrate"] = 128
with open(os.environ["TAPBOX_SETTINGS"], "w") as f:
    json.dump(saved, f)
assert sysinfo.load_settings()["spotify_bitrate"] == 160
print("5. corrupt saved value loads as the default, never as-is OK")

# 6. a config.yml without a bitrate line (pre-feature install) gets one
# appended rather than silently skipped
write_conf(bitrate_line="")
RESTARTS.clear()
output.set_spotify_bitrate(320)
assert conf_bitrate() and "320" in conf_bitrate(), conf_bitrate()
assert len(RESTARTS) == 1, RESTARTS
print("6. old config without the line: appended + restarted OK")

# 7. missing config.yml (go-librespot not installed yet): a calm no-op
os.remove(GO_CONF)
RESTARTS.clear()
output.set_spotify_bitrate(160)
assert RESTARTS == [], RESTARTS
print("7. no config file: quiet no-op OK")

# 8. the PWA wiring exists: the select is in the page and app.js binds it
html = open(os.path.join(REPO, "pi", "web", "index.html")).read()
js = open(os.path.join(REPO, "pi", "web", "app.js")).read()
assert 'id="set-bitrate"' in html and '"320"' in html
assert "#set-bitrate" in js and "spotify_bitrate" in js
print("8. PWA select present and bound to the setting OK")

print("SPOTIFY BITRATE OK — parent-adjustable from the phone, only "
      "rates go-librespot can play, restarts only on a real change.")
