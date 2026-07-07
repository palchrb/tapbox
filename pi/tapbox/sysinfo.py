"""Box settings (validated, consumers re-read live) and system status
(PiSugar battery, disk/cache usage, temperatures). Extracted verbatim
from daemon.py."""

import json
import os
import socket
import subprocess
import threading

from tapbox import netmgmt, output
from tapbox.paths import CACHE_DIR, SETTINGS_FILE


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def shutdown(restart=False):
    """Answer the HTTP request first, then power off."""
    cmd = ["reboot"] if restart else ["poweroff"]
    log(f"{'restart' if restart else 'shutdown'} requested")
    threading.Timer(1.0, lambda: subprocess.run(cmd)).start()
    return {"ok": True, "action": "restart" if restart else "poweroff"}


# --- settings (screen timeout, idle shutdown, volume cap) -----------------------

# Defaults double as the validation table: (default, min, max). 0 disables
# the screen timeout / idle shutdown.
SETTING_SPECS = {
    "screen_timeout_s": (30, 0, 600),
    "idle_shutdown_min": (30, 0, 240),
    "volume_cap": (100, 30, 100),
    "spotify_cache_gb": (20, 1, 100),
    "resume_on_boot": (1, 0, 1),
}


def load_settings():
    out = {k: spec[0] for k, spec in SETTING_SPECS.items()}
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        for k, spec in SETTING_SPECS.items():
            if isinstance(saved.get(k), (int, float)):
                out[k] = max(spec[1], min(spec[2], int(saved[k])))
    except (OSError, ValueError):
        pass
    return out


def update_settings(changes):
    if not isinstance(changes, dict):
        raise ValueError("settings must be an object")
    for k, v in changes.items():
        if k not in SETTING_SPECS:
            raise ValueError(f"unknown setting {k!r}")
        if not isinstance(v, (int, float)):
            raise ValueError(f"{k} must be a number")
        lo, hi = SETTING_SPECS[k][1], SETTING_SPECS[k][2]
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be {lo}-{hi}")
    merged = {**load_settings(), **{k: int(v) for k, v in changes.items()}}
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE + ".tmp", "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)
    log(f"settings updated: {changes}")
    if "spotify_cache_gb" in changes:
        output.resize_spotify_cache(merged["spotify_cache_gb"])
    return merged



# --- system status (battery, disk, wifi) ----------------------------------------

def pisugar_get(prop):
    """Query pisugar-server's TCP API, e.g. pisugar_get('battery') -> '84.2'."""
    try:
        with socket.create_connection(("127.0.0.1", 8423), timeout=2) as s:
            s.sendall(f"get {prop}\n".encode())
            s.settimeout(2)
            data = b""
            while b"\n" not in data:  # reply format: "battery: 84.2\n"
                chunk = s.recv(256)
                if not chunk:
                    break
                data += chunk
        text = data.decode(errors="ignore")
        return text.split(":", 1)[1].strip() if ":" in text else None
    except (OSError, IndexError):
        return None


def _safe_pct(raw):
    """A JSON-safe battery percentage: pisugar can return nan/inf while
    the charger toggles, and json.dumps(nan) is invalid JSON."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if -1 <= v <= 200 else None  # nan/inf fail the compare


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total




def system_status():
    batt = pisugar_get("battery")
    plugged = pisugar_get("battery_power_plugged")
    disk = None
    try:
        import shutil
        du = shutil.disk_usage(CACHE_DIR if os.path.isdir(CACHE_DIR) else "/")
        disk = {"total": du.total, "free": du.free}
    except OSError:
        pass
    caches = {}
    for name, p in (("podcasts", CACHE_DIR),
                    ("spotify", "/var/lib/tapbox/spotify-cache")):
        if os.path.isdir(p):
            caches[name] = _dir_size(p)
    temp = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = round(int(f.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        pass
    enabled, ssid, ip = netmgmt.wifi_state()
    return {"battery": _safe_pct(batt),
            "plugged": plugged == "true",
            "disk": disk, "caches": caches, "cpu_temp": temp,
            "wifi": {"enabled": enabled, "ssid": ssid, "ip": ip,
                     "hotspot": netmgmt.hotspot_active(),
                     "hotspot_ssid": netmgmt.HOTSPOT_SSID},
            "hostname": socket.gethostname()}


