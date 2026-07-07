"""Audio output plumbing shared by the daemon: which ALSA pcm is active
(bt speaker vs built-in/HAT), and the go-librespot config rewrites that
audio_device / cache size changes require. Extracted verbatim from daemon.py."""

import json
import os
import re
import subprocess

from tapbox.paths import STATE_DIR

OUT_FILE = os.path.join(STATE_DIR, "output.json")
GO_CONFIG = os.environ.get("TAPBOX_GO_CONFIG", "")  # go-librespot config.yml
OUTPUT_PCMS = {"bt": "tapbox_bt",
               "local": os.environ.get("TAPBOX_LOCAL_PCM", "tapbox_local")}


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def resize_spotify_cache(gb):
    """Write the size limit into go-librespot's config (startup-only there,
    like audio_device) and restart it. Eviction prunes on next start."""
    if not GO_CONFIG:
        return
    try:
        with open(GO_CONFIG) as f:
            text = f.read()
    except OSError:
        return
    new, n = re.subn(r"(?m)^(\s*size_limit:).*$", rf"\g<1> {gb}GB", text, count=1)
    if n == 0 or new == text:
        return
    with open(GO_CONFIG + ".tmp", "w") as f:
        f.write(new)
    os.replace(GO_CONFIG + ".tmp", GO_CONFIG)
    log(f"spotify cache limit -> {gb}GB (restarting go-librespot)")
    try:
        subprocess.run(["systemctl", "restart", "go-librespot"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart failed ({e!r}) — restart it manually")


# --- audio output (bt speaker vs built-in/HAT) ----------------------------------

def _i2s_card_present():
    try:
        with open("/proc/asound/cards") as f:
            return "sndrpihifiberry" in f.read()
    except OSError:
        return False


def current_output():
    try:
        with open(OUT_FILE) as f:
            d = json.load(f)
        return {"output": d.get("output") or "bt",
                "pcm": d.get("pcm") or "tapbox_bt"}
    except (OSError, ValueError):
        return {"output": "bt", "pcm": "tapbox_bt"}


def _retarget_go_librespot(pcm):
    """Point go-librespot's audio_device at pcm. Unlike mpv, its audio
    device is startup config — a change means config rewrite + restart.
    Returns True when the config was changed."""
    if not GO_CONFIG:
        return False
    try:
        with open(GO_CONFIG) as f:
            text = f.read()
    except OSError:
        return False
    new, n = re.subn(r"(?m)^audio_device:.*$", f"audio_device: {pcm}", text)
    if n == 0:
        new = text.rstrip("\n") + f"\naudio_device: {pcm}\n"
    if new == text:
        return False
    with open(GO_CONFIG + ".tmp", "w") as f:
        f.write(new)
    os.replace(GO_CONFIG + ".tmp", GO_CONFIG)
    try:
        subprocess.run(["systemctl", "restart", "go-librespot"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart failed ({e!r}) — config updated, "
            "restart it manually")
    return True


