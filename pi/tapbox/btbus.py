"""Bluetooth TRANSPORT layer — one narrow primitive surface, two backends.

  cli   bluetoothctl / bluealsa-aplay text parsing (the proven path)
  dbus  direct BlueZ + bluealsa D-Bus (PLAN-bt-dbus.md; preferred by
        `auto` since the parity gate passed on the rig 2026-07-07)

Selected once per process via TAPBOX_BT_BACKEND=cli|dbus|auto (default
auto). Flow logic — pairing retries, stale-key handling, one-output
policy, MAC_FILE/ASOUND routing — lives in bt.py and never sees which
backend ran. Error CLASSIFICATION is the contract here: pair() returns
one of the PAIR_* constants; the cli backend maps regexes, the dbus
backend maps typed org.bluez.Error.* names.

Phase status (PLAN-bt-dbus.md §3): the dbus backend currently covers
the READ primitives (A1); action primitives delegate to the cli
implementation until phase B lands. Importing this module must never
require dbus — all dbus imports are lazy.
"""

import os
import re
import subprocess
import time

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# pair() classification — the contract bt.py's flow logic branches on
PAIR_OK = "ok"
PAIR_ALREADY = "already-paired"
PAIR_AUTH_FAILED = "auth-failed"     # stale key: clearing the bond is right
PAIR_NOT_AVAILABLE = "not-available"  # never seen during scan
PAIR_ERROR = "error"

# remove_device() classification
REMOVE_OK = "ok"
REMOVE_NOT_FOUND = "not-found"       # already gone: treated as success
REMOVE_ERROR = "error"


def log(msg):
    print(msg, flush=True)


def _run(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, _ANSI.sub("", r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return 1, "(timed out)"
    except FileNotFoundError as e:
        return 127, str(e)


def _ctl(*args, timeout=30):
    return _run(["bluetoothctl", *args], timeout=timeout)


# --- backend selection -----------------------------------------------------------

_BACKEND = None  # resolved once per process


def backend():
    global _BACKEND
    if _BACKEND is None:
        want = os.environ.get("TAPBOX_BT_BACKEND", "auto")
        if want == "cli":
            _BACKEND = "cli"  # explicit kill switch
        else:
            # auto prefers dbus since the parity gate passed on the rig
            # (2026-07-07, tests/bt_parity.py: PARITY OK). Every dbus
            # read still degrades to the cli path on any bus failure.
            try:
                import dbus  # noqa: F401 — availability probe only
                _BACKEND = "dbus"
                log(f"bt backend: dbus ({want})")
            except ImportError:
                log("bt backend: dbus unavailable — using bluetoothctl")
                _BACKEND = "cli"
    return _BACKEND


def _dbus_read(fn_dbus, fn_cli, *args):
    """Run the dbus read primitive with the cli one as a safety net —
    a bus hiccup must degrade exactly like a bluetoothctl failure."""
    if backend() == "dbus":
        try:
            return fn_dbus(*args)
        except Exception as e:  # DBusException, missing name, ...
            log(f"bt dbus read failed ({e.__class__.__name__}) — cli fallback")
    return fn_cli(*args)


# --- primitives: adapter ---------------------------------------------------------

def adapter_power_on():
    if backend() == "dbus":
        try:
            _dbus_adapter_set("Powered", True)
            return
        except Exception as e:
            log(f"bt dbus power-on failed ({e.__class__.__name__}) — cli")
    _ctl("power", "on")


def adapter_pairable_on():
    """Without pairable on, BlueZ does a NON-BONDING pairing (key thrown
    away, bond gone after power cycle) — learned on real hardware."""
    if backend() == "dbus":
        try:
            _dbus_adapter_set("Pairable", True)
            return
        except Exception as e:
            log(f"bt dbus pairable failed ({e.__class__.__name__}) — cli")
    _ctl("pairable", "on")


def adapter_powered():
    return _dbus_read(_dbus_adapter_powered, _cli_adapter_powered)


def _cli_adapter_powered():
    _c, out = _ctl("show", timeout=10)
    return "Powered: yes" in out


# --- primitives: device listing / info -------------------------------------------

def paired_devices():
    """[{mac, name}] for every bonded device."""
    return _dbus_read(_dbus_paired_devices, _cli_paired_devices)


def connected_devices():
    return _dbus_read(_dbus_connected_devices, _cli_connected_devices)


def _cli_device_lines(args):
    _c, out = _ctl(*args, timeout=10)
    if args == ("devices", "Paired") and ("Invalid" in out or "Unknown" in out):
        _c, out = _ctl("paired-devices", timeout=10)  # older bluez
    devices = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[0] == "Device":
            devices.append({"mac": parts[1],
                            "name": parts[2] if len(parts) > 2 else parts[1]})
    return devices


def _cli_paired_devices():
    return _cli_device_lines(("devices", "Paired"))


def _cli_connected_devices():
    return _cli_device_lines(("devices", "Connected"))


def device_info(mac):
    """{present, paired, connected, name} — present=False means BlueZ has
    no object for the device at all (never seen / removed)."""
    return _dbus_read(_dbus_device_info, _cli_device_info, mac)


def _cli_device_info(mac):
    _c, info = _ctl("info", mac, timeout=10)
    if not info.strip() or "not available" in info.lower():
        return {"present": False, "paired": False, "connected": False,
                "name": None}
    m = re.search(r"^\s*(?:Alias|Name): (.+)$", info, re.M)
    return {"present": True,
            "paired": "Paired: yes" in info,
            "connected": "Connected: yes" in info,
            "name": m.group(1).strip() if m else None}


# --- primitives: discovery -------------------------------------------------------

def populate_cache(secs):
    """Let BlueZ see the device before pair() — no result needed."""
    if backend() == "dbus":
        try:
            _dbus_discover(secs)
            return
        except Exception as e:
            log(f"bt dbus scan failed ({e.__class__.__name__}) — cli")
    _ctl("--timeout", str(secs), "scan", "on", timeout=secs + 15)


def discover(secs):
    """[{mac, name, audio, rssi}] for devices actually seen THIS window
    (BlueZ's cache of long-gone devices must never leak into pickers).
    rssi is None on the cli backend."""
    return _dbus_read(_dbus_discover, _cli_discover, secs)


def _cli_discover(secs):
    _c, out = _ctl("--timeout", str(secs), "scan", "on", timeout=secs + 15)
    macs = sorted({m.group(1).upper() for m in re.finditer(
        r"Device ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", out)})
    found = []
    for mac in macs:
        _c, info = _ctl("info", mac, timeout=10)
        m = re.search(r"^\s*Name: (.+)$", info, re.M)
        name = m.group(1).strip() if m else "(no name)"
        audio = bool(re.search(r"Icon: audio|Audio Sink|0000110b", info, re.I))
        found.append({"mac": mac, "name": name, "audio": audio, "rssi": None})
    return found


# --- primitives: actions (dbus versions land in phase B) -------------------------

def pair(mac):
    """(classification, raw_output_for_log)."""
    if backend() == "dbus":
        log("bt dbus: pair via bluetoothctl until phase B")  # PLAN §3 B2
    code, out = _ctl("pair", mac, timeout=45)
    if code == 0:
        return PAIR_OK, out
    if re.search("AlreadyExists", out, re.I):
        return PAIR_ALREADY, out
    if re.search("AuthenticationFailed|AuthenticationCanceled|"
                 "AuthenticationRejected|AuthenticationTimeout", out, re.I):
        return PAIR_AUTH_FAILED, out
    if re.search("not available", out, re.I):
        return PAIR_NOT_AVAILABLE, out
    return PAIR_ERROR, out


def trust(mac):
    _ctl("trust", mac, timeout=15)


def connect_device(mac):
    """(ok, detail) — one attempt; retries are flow logic in bt.py."""
    code, out = _ctl("connect", mac, timeout=30)
    return code == 0, out


def disconnect_device(mac):
    code, out = _ctl("disconnect", mac, timeout=15)
    return code == 0, out


def remove_device(mac):
    code, out = _ctl("remove", mac, timeout=15)
    if code == 0:
        return REMOVE_OK, out
    if "not available" in out.lower():
        return REMOVE_NOT_FOUND, out
    return REMOVE_ERROR, out


# --- primitives: bluealsa --------------------------------------------------------

def a2dp_pcm_present(mac):
    """The real 'audio ready' signal: bluealsa exposes an A2DP PCM."""
    if backend() == "dbus":
        try:
            return _dbus_a2dp_pcm_present(mac)
        except Exception as e:
            log(f"bt dbus pcm check failed ({e.__class__.__name__}) — cli")
    return _cli_a2dp_pcm_present(mac)


def _cli_a2dp_pcm_present(mac):
    _c, pcm = _run(["bluealsa-aplay", "-L"], timeout=10)
    return mac.lower() in pcm.lower()


# --- dbus backend ----------------------------------------------------------------
# Read primitives only (phase A1). All imports lazy; every entry point is
# wrapped by callers so any DBusException degrades to the cli path.
# Verified against BlueZ 5.82 API; exact bluealsa path grammar is on the
# rig checklist (PLAN-bt-dbus.md §6).

_BLUEZ = "org.bluez"
_ADAPTER_PATH = "/org/bluez/hci0"
_BLUEALSA = "org.bluealsa"


def _bus():
    import dbus
    addr = (os.environ.get("TAPBOX_DBUS_ADDRESS")
            or os.environ.get("DBUS_SYSTEM_BUS_ADDRESS"))
    if addr:
        # explicit connection: the test harness's private bus must win
        # even where libdbus ignores the env (setuid, scrubbing, ...)
        return dbus.bus.BusConnection(addr)
    return dbus.SystemBus()


def _managed(service, path="/"):
    import dbus
    om = dbus.Interface(_bus().get_object(service, path),
                        "org.freedesktop.DBus.ObjectManager")
    return om.GetManagedObjects(timeout=10)


def _dbus_adapter_props():
    import dbus
    return dbus.Interface(_bus().get_object(_BLUEZ, _ADAPTER_PATH),
                          "org.freedesktop.DBus.Properties")


def _dbus_adapter_set(prop, value):
    import dbus
    _dbus_adapter_props().Set("org.bluez.Adapter1", prop,
                              dbus.Boolean(value), timeout=10)


def _dbus_adapter_powered():
    return bool(_dbus_adapter_props().Get("org.bluez.Adapter1", "Powered",
                                          timeout=10))


def _dbus_device_list(prop):
    out = []
    for _path, ifaces in _managed(_BLUEZ).items():
        dev = ifaces.get("org.bluez.Device1")
        if dev and bool(dev.get(prop)):
            mac = str(dev.get("Address", "")).upper()
            out.append({"mac": mac, "name": str(dev.get("Alias") or mac)})
    return sorted(out, key=lambda d: d["mac"])


def _dbus_paired_devices():
    return _dbus_device_list("Paired")


def _dbus_connected_devices():
    return _dbus_device_list("Connected")


def _dev_path(mac):
    return _ADAPTER_PATH + "/dev_" + mac.upper().replace(":", "_")


def _dbus_device_info(mac):
    import dbus
    try:
        props = dbus.Interface(_bus().get_object(_BLUEZ, _dev_path(mac)),
                               "org.freedesktop.DBus.Properties")
        d = props.GetAll("org.bluez.Device1", timeout=10)
    except dbus.exceptions.DBusException as e:
        name = e.get_dbus_name() or ""
        # real bluez: UnknownObject; dbus-python fakes: UnknownMethod —
        # both mean "no such device object", and absence is authoritative
        if "UnknownObject" in name or "UnknownMethod" in name:
            return {"present": False, "paired": False, "connected": False,
                    "name": None}
        raise
    return {"present": True,
            "paired": bool(d.get("Paired")),
            "connected": bool(d.get("Connected")),
            "name": str(d.get("Alias")) if d.get("Alias") else None}


_AUDIO_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"


def _dbus_is_audio(dev):
    icon = str(dev.get("Icon", ""))
    if icon.startswith("audio"):
        return True
    if _AUDIO_SINK_UUID in [str(u).lower() for u in dev.get("UUIDs", [])]:
        return True
    # Class major device class 0x04 = audio/video — works pre-SDP, when
    # UUIDs are still empty for unpaired devices
    try:
        return (int(dev.get("Class", 0)) >> 8) & 0x1F == 0x04
    except (TypeError, ValueError):
        return False


def _dbus_discover(secs):
    """Loop-free discovery: RSSI is only present on devices seen during
    an active discovery, so 'has RSSI' (or 'path appeared after start')
    gates out BlueZ's cache of long-gone devices without needing signal
    subscriptions (those come with the phase C daemon)."""
    import dbus
    adapter = dbus.Interface(_bus().get_object(_BLUEZ, _ADAPTER_PATH),
                             "org.bluez.Adapter1")
    before = set(_managed(_BLUEZ).keys())
    try:
        adapter.SetDiscoveryFilter({"Transport": "bredr"}, timeout=10)
    except dbus.exceptions.DBusException:
        pass  # filter is best-effort (another client may be scanning)
    started = True
    try:
        adapter.StartDiscovery(timeout=10)
    except dbus.exceptions.DBusException as e:
        if "InProgress" not in (e.get_dbus_name() or ""):
            raise
        started = False  # ride along on the other client's discovery
    seen = {}
    deadline = time.monotonic() + secs
    try:
        while time.monotonic() < deadline:
            time.sleep(2)
            for path, ifaces in _managed(_BLUEZ).items():
                dev = ifaces.get("org.bluez.Device1")
                if not dev:
                    continue
                fresh = "RSSI" in dev or path not in before
                if not fresh:
                    continue
                mac = str(dev.get("Address", "")).upper()
                seen[mac] = {
                    "mac": mac,
                    "name": str(dev.get("Alias") or mac),
                    "audio": _dbus_is_audio(dev),
                    "rssi": int(dev["RSSI"]) if "RSSI" in dev else None,
                }
    finally:
        if started:
            try:
                adapter.StopDiscovery(timeout=10)
            except Exception:
                pass  # discovery dies with our connection anyway
    # strongest signal first, unknown-RSSI last (sort/display only —
    # pairing safety rules live in bt.py and never auto-pick by RSSI)
    return sorted(seen.values(),
                  key=lambda d: -(d["rssi"] if d["rssi"] is not None else -999))


def _dbus_a2dp_pcm_present(mac):
    """bluealsa exposes PCM1 objects under /org/bluealsa; ours is the
    a2dp source->sink for the device. Presence IS the ready signal."""
    frag = "/dev_" + mac.upper().replace(":", "_") + "/"
    for path, ifaces in _managed(_BLUEALSA, "/org/bluealsa").items():
        pcm = ifaces.get("org.bluealsa.PCM1")
        if pcm is None or frag not in str(path):
            continue
        if str(pcm.get("Mode", "sink")) == "sink":
            return True
    return False
