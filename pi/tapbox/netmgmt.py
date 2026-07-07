"""WiFi management via nmcli (Bookworm's NetworkManager): scan/join/forget,
the setup hotspot with its fresh-box watchdog, and the rfkill on/off toggle.
Extracted verbatim from daemon.py."""

import os
import re
import socket
import subprocess
import threading
import time


def log(msg):
    print(f"tapboxd: {msg}", flush=True)


def _run_out(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def wifi_state():
    """(enabled, ssid, ip) — enabled means not rfkill-blocked."""
    out = _run_out(["rfkill", "list", "wifi"]).lower()
    enabled = "blocked: yes" not in out  # soft or hard block = off
    ssid = None
    for line in _run_out(["iw", "dev", "wlan0", "link"]).splitlines():
        if line.strip().startswith("SSID:"):
            ssid = line.split(":", 1)[1].strip()
    ip = (_run_out(["hostname", "-I"]).split() or [None])[0]
    return enabled, ssid, ip


def _rfkill(enabled):
    try:
        subprocess.run(["rfkill", "unblock" if enabled else "block", "wifi"],
                       timeout=10)
        return None
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)


def set_wifi(enabled):
    """User-facing switch (PWA/screen). Turning wifi ON also grants a fresh
    auto-off grace window; turning it OFF marks the block as deliberate so
    the auto-off prober won't sneak it back on."""
    err = _rfkill(enabled)
    if err:
        return {"error": err}
    log(f"wifi {'unblock' if enabled else 'block'}ed")
    now = time.monotonic()
    if enabled:
        _auto.update(last_ok=now, blocked=False)
    else:
        _auto.update(blocked=False)
    en, ssid, ip = wifi_state()
    return {"enabled": en, "ssid": ssid, "ip": ip}


# --- wifi management (nmcli — Bookworm's NetworkManager) --------------------------

WIFI_LOCK = threading.Lock()  # one scan/connect at a time
HOTSPOT_CON = "tapbox-hotspot"
HOTSPOT_SSID = os.environ.get("TAPBOX_HOTSPOT_SSID") \
    or f"TapBox-{socket.gethostname()}"
HOTSPOT_PSK = os.environ.get("TAPBOX_HOTSPOT_PSK", "tapbox123")
WATCHDOG_DELAY_S = int(os.environ.get("TAPBOX_WIFI_WATCHDOG_DELAY", "45"))
# wifi auto-off: a disconnected wpa_supplicant scan-loops constantly
# (~10-20mA — 5-10% of playback draw); after wifi_auto_off_min without a
# known network we rfkill-block, then briefly probe every PROBE_INTERVAL
# so a parent's hotspot at the cabin is still found within ~10 minutes.
PROBE_INTERVAL_S = int(os.environ.get("TAPBOX_WIFI_PROBE_INTERVAL", "600"))
PROBE_WINDOW_S = int(os.environ.get("TAPBOX_WIFI_PROBE_WINDOW", "30"))
_auto = {"last_ok": 0.0, "blocked": False, "next_probe": 0.0}
_last_scan = {"networks": [], "at": 0.0}  # wlan0 can't scan while in AP mode


def _nmcli(*args, timeout=60):
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "nmcli not found — this box does not use NetworkManager"
    except subprocess.TimeoutExpired:
        return 1, "nmcli timed out"


def _nm_unescape(s):
    """nmcli -t escapes ':' and '\\' in field values."""
    return s.replace("\\\\", "\0").replace("\\:", ":").replace("\0", "\\")


def _known_wifi_names():
    _code, out = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show",
                        timeout=10)
    known = set()
    for line in out.splitlines():
        name, _, ctype = line.rpartition(":")
        if ctype == "802-11-wireless":
            known.add(_nm_unescape(name))
    return known


def hotspot_active():
    _code, out = _nmcli("-t", "-f", "NAME", "connection", "show", "--active",
                        timeout=10)
    return HOTSPOT_CON in [_nm_unescape(x) for x in out.splitlines()]


def start_hotspot():
    """Bring up the setup AP. Scans first — the radio can't scan in AP mode,
    so the portal's network picker serves this cached list."""
    sc = wifi_scan()
    if sc and sc.get("ok") and sc.get("networks"):
        _last_scan.update(networks=sc["networks"], at=time.time())
    code, out = _nmcli("dev", "wifi", "hotspot", "ifname", "wlan0",
                       "con-name", HOTSPOT_CON, "ssid", HOTSPOT_SSID,
                       "password", HOTSPOT_PSK, timeout=30)
    log(f"hotspot {HOTSPOT_SSID}: {'up' if code == 0 else 'FAILED: ' + out.splitlines()[-1] if out else 'FAILED'}")
    return code == 0


def stop_hotspot():
    _nmcli("connection", "down", HOTSPOT_CON, timeout=15)
    _nmcli("connection", "delete", "id", HOTSPOT_CON, timeout=15)
    log("hotspot stopped")


def _link_up():
    try:
        with open("/sys/class/net/wlan0/operstate") as f:
            return f.read().strip() == "up"
    except OSError:
        return False


def _wifi_watchdog():
    """Two jobs, both keyed on 'the link is down':

    Fresh-box onboarding: no saved wifi network and nothing connected
    -> start the setup hotspot. Boxes WITH saved networks never auto-AP
    (a cabin trip must not burn battery on a pointless hotspot) — there
    the PWA/screen button starts it explicitly.

    Wifi auto-off: a box with saved networks that can't find any of them
    scan-loops for nothing; after wifi_auto_off_min (0 = never) we block
    the radio, then re-probe for PROBE_WINDOW_S every PROBE_INTERVAL_S and
    stay on only when a known network actually takes us in. Turning wifi
    on via PWA/screen (set_wifi) always grants a fresh grace window; a
    manual 'wifi off' is never probed back on. Never triggers during
    playback of streams by construction — streaming means the link is up."""
    from tapbox.sysinfo import load_settings
    time.sleep(WATCHDOG_DELAY_S)
    _auto["last_ok"] = time.monotonic()
    while True:
        try:
            # Cheap first: one sysfs read. Only when the link is down do we
            # pay for the rfkill/iw/nmcli subprocess probes — a battery box
            # must not spawn processes every 30s around the clock.
            now = time.monotonic()
            if _link_up():
                _auto.update(last_ok=now, blocked=False)
                time.sleep(30)
                continue
            enabled, ssid, _ip = wifi_state()
            if enabled:
                if ssid or hotspot_active():
                    _auto.update(last_ok=now, blocked=False)
                elif not _known_wifi_names():
                    log("no saved wifi + not connected — starting setup "
                        "hotspot")
                    start_hotspot()
                else:
                    auto_min = load_settings().get("wifi_auto_off_min", 0)
                    if auto_min and now - _auto["last_ok"] > auto_min * 60:
                        log(f"no known network for {auto_min} min — "
                            f"wifi off (probing every "
                            f"{PROBE_INTERVAL_S // 60} min)")
                        _rfkill(False)
                        _auto.update(blocked=True,
                                     next_probe=now + PROBE_INTERVAL_S)
            elif _auto["blocked"] and now >= _auto["next_probe"]:
                log("wifi probe: looking for known networks")
                _rfkill(True)  # NetworkManager scans + auto-joins known nets
                found = None
                deadline = time.monotonic() + PROBE_WINDOW_S
                while time.monotonic() < deadline and not found:
                    time.sleep(5)
                    _en, found, _ = wifi_state()
                if found:
                    log(f"wifi probe: reconnected to {found!r}")
                    _auto.update(last_ok=time.monotonic(), blocked=False)
                else:
                    log("wifi probe: nothing known nearby — off again")
                    _rfkill(False)
                    _auto["next_probe"] = time.monotonic() + PROBE_INTERVAL_S
        except Exception as e:
            log(f"wifi watchdog error: {e!r}")
        time.sleep(30)


def wifi_scan():
    """Nearby networks, strongest first. None = busy."""
    if not WIFI_LOCK.acquire(blocking=False):
        return None
    try:
        if hotspot_active():  # AP mode: serve the pre-hotspot scan
            return {"ok": True, "cached": True, "hotspot": True,
                    "networks": _last_scan["networks"]}
        code, out = _nmcli("-t", "-f", "IN-USE,SIGNAL,SECURITY,SSID",
                           "dev", "wifi", "list", "--rescan", "yes",
                           timeout=30)
        if code != 0:
            return {"ok": False, "networks": [],
                    "output": out.splitlines()[-1] if out else "scan failed"}
        nets = {}
        for line in out.splitlines():
            parts = line.split(":", 3)  # SSID last -> its colons survive
            if len(parts) != 4:
                continue
            in_use, signal, security, ssid = parts
            ssid = _nm_unescape(ssid)
            if not ssid:
                continue  # hidden network
            entry = {"ssid": ssid,
                     "signal": int(signal) if signal.isdigit() else 0,
                     "secured": bool(security and security != "--"),
                     "in_use": in_use == "*"}
            cur = nets.get(ssid)  # several BSSIDs -> keep the strongest
            if cur is None or entry["signal"] > cur["signal"]:
                if cur and cur["in_use"]:
                    entry["in_use"] = True
                nets[ssid] = entry
        known = _known_wifi_names()
        for n in nets.values():
            n["known"] = n["ssid"] in known
        return {"ok": True,
                "networks": sorted(nets.values(),
                                   key=lambda n: (-n["in_use"], -n["signal"]))}
    finally:
        WIFI_LOCK.release()


def wifi_connect(ssid, password=None):
    """Join a network (uses the saved profile when one exists). None = busy."""
    if not WIFI_LOCK.acquire(blocking=False):
        return None
    try:
        was_hotspot = hotspot_active()
        if was_hotspot:
            log("leaving the setup hotspot to join a network...")
            _nmcli("connection", "down", HOTSPOT_CON, timeout=15)
        if password:
            code, out = _nmcli("dev", "wifi", "connect", ssid,
                               "password", password, timeout=75)
        elif ssid in _known_wifi_names():
            code, out = _nmcli("connection", "up", "id", ssid, timeout=75)
        else:
            code, out = _nmcli("dev", "wifi", "connect", ssid, timeout=75)
        tail = "\n".join(out.splitlines()[-3:])
        log(f"wifi connect {ssid!r} -> exit {code}")
        if code != 0 and was_hotspot:
            # Let the user retry from the portal instead of stranding them
            _nmcli("connection", "up", HOTSPOT_CON, timeout=30) \
                if _hotspot_profile_exists() else start_hotspot()
            tail += "\nsetup hotspot restored — reconnect and retry"
        enabled, cur, ip = wifi_state()
        return {"ok": code == 0, "output": tail, "ssid": cur, "ip": ip}
    finally:
        WIFI_LOCK.release()


def _hotspot_profile_exists():
    _c, out = _nmcli("-t", "-f", "NAME", "connection", "show", timeout=10)
    return HOTSPOT_CON in [_nm_unescape(x) for x in out.splitlines()]


def wifi_forget(ssid):
    if not WIFI_LOCK.acquire(blocking=False):
        return None
    try:
        code, out = _nmcli("connection", "delete", "id", ssid, timeout=15)
        log(f"wifi forget {ssid!r} -> exit {code}")
        return {"ok": code == 0,
                "output": "\n".join(out.splitlines()[-2:])}
    finally:
        WIFI_LOCK.release()


