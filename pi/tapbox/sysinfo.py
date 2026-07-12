"""Box settings (validated, consumers re-read live) and system status
(PiSugar battery, disk/cache usage, temperatures). Extracted verbatim
from daemon.py."""

import json
import os
import socket
import subprocess
import threading
import time

from tapbox import netmgmt, output
from tapbox.paths import CACHE_DIR, SETTINGS_FILE, STATE_DIR


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
    "screen_brightness": (100, 10, 100),  # % backlight (min 10: never black)
    "idle_shutdown_min": (30, 0, 240),
    "volume_cap": (100, 30, 100),
    "spotify_cache_gb": (20, 1, 100),
    "resume_on_boot": (1, 0, 1),
    "wifi_auto_off_min": (15, 0, 240),  # 0 = never auto-off
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

_PISUGAR_SOCK = [None]  # persistent connection (guarded by the lock)
_PISUGAR_LOCK = threading.Lock()


def _pisugar_drop():
    s, _PISUGAR_SOCK[0] = _PISUGAR_SOCK[0], None
    if s is not None:
        try:
            s.close()
        except OSError:
            pass


def pisugar_get(prop):
    """Query pisugar-server's TCP API, e.g. pisugar_get('battery') -> '84.2'.

    One persistent connection: pisugar-server logs every connect (2x INFO)
    and treats every disconnect as an error ('Response error: Stream
    closed' WARN) — with the PWA battery pill polling, per-request
    connections flooded the journal with 6 lines per refresh."""
    with _PISUGAR_LOCK:
        for attempt in (1, 2):  # second try = fresh connection
            try:
                s = _PISUGAR_SOCK[0]
                if s is None:
                    s = socket.create_connection(("127.0.0.1", 8423),
                                                 timeout=2)
                    _PISUGAR_SOCK[0] = s
                s.settimeout(2)
                s.sendall(f"get {prop}\n".encode())
                data = b""
                while b"\n" not in data:  # reply: "battery: 84.2\n"
                    chunk = s.recv(256)
                    if not chunk:
                        raise OSError("pisugar closed the connection")
                    data += chunk
                text = data.decode(errors="ignore").strip()
                if not text.startswith(prop):
                    # desynced (a stale reply from an earlier timeout) —
                    # drop the connection rather than mismatch answers
                    raise OSError(f"unexpected reply {text[:40]!r}")
                return text.split(":", 1)[1].strip() if ":" in text else None
            except (OSError, IndexError):
                _pisugar_drop()
                if attempt == 2:
                    return None


def _safe_volts(raw):
    """JSON-safe battery voltage; sane Li-Ion range only (nan/inf fail)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 2) if 2.0 <= v <= 6.0 else None


def _safe_pct(raw):
    """A JSON-safe battery percentage: pisugar can return nan/inf while
    the charger toggles, and json.dumps(nan) is invalid JSON."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if -1 <= v <= 200 else None  # nan/inf fail the compare


_PCT_HIST = []  # last readings — the percent is voltage-modelled and sags
                # a few points under load; median stops the visible bounce


def _smoothed_pct(pct, plugged):
    if pct is None or plugged:
        # charging moves fast and monotonically — show it raw
        _PCT_HIST.clear()
        return pct
    _PCT_HIST.append(pct)
    del _PCT_HIST[:-3]
    return sorted(_PCT_HIST)[len(_PCT_HIST) // 2]


BATT_RUNTIME_FILE = os.path.join(STATE_DIR, "on-battery-runtime.json")


CHARGE_RESET_PCT = 5  # a battery level that ROSE this much means the charger
                      # was on — even if 'plugged' lied, or the charge happened
                      # entirely while the box was powered off (tracker asleep)


def _load_runtime():
    """(accum_seconds, last_pct) from disk, or (None, None)."""
    try:
        with open(BATT_RUNTIME_FILE) as f:
            d = json.load(f)
        return max(0, int(d["accum"])), d.get("last_pct")
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def _battery_runtime():
    """Accumulated POWERED-ON seconds since the last charge, or None while
    on the charger / without a PiSugar. The box can be switched off in
    between — wall-clock would count sleep as usage, so a daemon thread
    accumulates actual uptime instead (persisted across restarts)."""
    return _load_runtime()[0]


def _runtime_step(delta, plugged, charging, pct, prev_accum, prev_pct):
    """One tick's decision (pure). Returns None to reset the counter, else
    (accum_seconds, last_pct) to persist. Any sign of charging resets:
    plugged in, actively charging, or a battery level that rose past the
    noise floor (a charge that slipped past, incl. one while powered off)."""
    rose = (pct is not None and prev_pct is not None
            and pct > prev_pct + CHARGE_RESET_PCT)
    if plugged == "true" or charging == "true" or rose:
        return None
    return int((prev_accum or 0) + delta), pct


def _battery_runtime_tracker():
    """60s ticks: while on battery, add the elapsed powered-on time to the
    persisted counter; reset it whenever the box is (or was) charging.

    Charging is detected three ways so the counter can't run away: the
    charger is plugged in, the pack is actively charging, OR the battery
    level has risen since we last looked. That last signal is what catches
    a charge that happened while the box was switched OFF (this thread
    wasn't running to see 'plugged'), which otherwise let the counter add
    session onto session into implausible totals."""
    last = time.monotonic()
    while True:
        time.sleep(60)
        try:
            now = time.monotonic()
            delta, last = now - last, now
            plugged = pisugar_get("battery_power_plugged")
            if plugged is None:
                continue  # no pisugar on this box (or a transient read miss)
            charging = pisugar_get("battery_charging")
            pct = _safe_pct(pisugar_get("battery"))
            prev_accum, prev_pct = _load_runtime()
            step = _runtime_step(delta, plugged, charging, pct,
                                 prev_accum, prev_pct)
            if step is None:
                try:
                    os.remove(BATT_RUNTIME_FILE)
                except OSError:
                    pass
                continue
            accum, last_pct = step
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(BATT_RUNTIME_FILE + ".tmp", "w") as f:
                json.dump({"accum": int(accum), "last_pct": last_pct}, f)
            os.replace(BATT_RUNTIME_FILE + ".tmp", BATT_RUNTIME_FILE)
        except Exception as e:
            log(f"battery runtime tracker error: {e!r}")


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
    volts = pisugar_get("battery_v")
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
    on_battery_s = None
    if batt is not None and plugged != "true":
        on_battery_s = _battery_runtime()
    return {"battery": _smoothed_pct(_safe_pct(batt), plugged == "true"),
            "battery_v": _safe_volts(volts),
            "on_battery_s": on_battery_s,
            "plugged": plugged == "true",
            "disk": disk, "caches": caches, "cpu_temp": temp,
            "wifi": {"enabled": enabled, "ssid": ssid, "ip": ip,
                     "hotspot": netmgmt.hotspot_active(),
                     "hotspot_ssid": netmgmt.HOTSPOT_SSID},
            "hostname": socket.gethostname()}


