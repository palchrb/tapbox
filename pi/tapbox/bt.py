#!/usr/bin/env python3
"""Bluetooth speaker management — play.sh's hard-won BlueZ workarounds,
ported verbatim (step 1 of the bt refactor; step 2 will swap the
bluetoothctl text-parsing internals for the BlueZ D-Bus API).

The workarounds this preserves, all learned on real hardware:
- rfkill soft-block persists across reboots on fresh installs -> unblock
- without `pairable on`, BlueZ does a NON-BONDING pairing (new_link_key
  with store_hint 0): "Pairing successful" but the key is thrown away,
  so the bond is gone after a power cycle
- `bluetoothctl info` can intermittently return nothing (D-Bus hiccup)
  -> retry before concluding the device is unpaired
- pair failures are classified: AlreadyExists = fine, continue;
  AuthenticationFailed = stale key on the device, and ONLY then is it
  right to clear our bond and pair fresh; "not available" = not seen
- "Connected: yes" right after pairing is just the pairing link — a
  profile connect is still required, and the real "ready" signal is
  bluealsa exposing an A2DP PCM for the device

CLI (used by play.sh and tapboxd's /bt endpoints):
  bt.py scan            human-readable device list
  bt.py scan-raw        mac<TAB>name<TAB>yes|no  (audio confirmed?)
  bt.py connect [name]  auto-pair: the single audio device in pairing mode
  bt.py use <MAC>       connect a device (pairs first when unknown)
  bt.py forget <MAC>    drop the bond (clears config if it was active)
  bt.py ensure          connect the remembered device, else auto-pair
"""

import os
import re
import subprocess
import sys
import threading
import time

MAC_FILE = os.environ.get("TAPBOX_BT_FILE", "/etc/tapbox/bt-headset")
ASOUND = os.environ.get("TAPBOX_ASOUND", "/etc/asound.conf")
SCAN_SECS = int(os.environ.get("TAPBOX_SCAN_SECS", "20"))
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


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


def btctl(*args, timeout=30):
    return _run(["bluetoothctl", *args], timeout=timeout)


def bt_up():
    """Radio can be rfkill-blocked (persists across reboots on a fresh
    install), which makes every scan come up empty."""
    _run(["rfkill", "unblock", "bluetooth"], timeout=10)
    btctl("power", "on")
    if not controller_ok():
        recover()
    btctl("pairable", "on")  # bonding pairing — see module docstring


def _hci_crashed():
    """The crash leaves bluez believing the adapter is fine ('Powered:
    yes' is stale state), so ask the kernel: the firmware crash has an
    unmistakable signature in the log."""
    _c, out = _run(["dmesg"], timeout=10)
    tail = "\n".join(out.splitlines()[-80:])
    return "hci0: hardware error" in tail or "Opcode 0x0c03 failed" in tail


def controller_ok():
    _c, out = btctl("show", timeout=10)
    return "Powered: yes" in out


def recover():
    """The Zero 2 W's BT controller can crash outright (kernel logs
    'Bluetooth: hci0: hardware error 0x00', typically under 2.4GHz
    wifi/BT coexistence load); after that every HCI command times out
    ('Opcode 0x0c03 failed: -110') until the firmware is re-attached —
    a reboot used to be the only cure. Re-init the whole chain instead:
    hciuart re-uploads the firmware, then bluetooth + bluealsa return."""
    log("==> Bluetooth controller looks dead — re-attaching firmware...")
    _run(["systemctl", "stop", "bluetooth"], timeout=30)
    _run(["systemctl", "restart", "hciuart"], timeout=60)
    _run(["systemctl", "start", "bluetooth"], timeout=30)
    for unit in ("bluealsa", "bluealsad"):  # name differs across releases
        _run(["systemctl", "try-restart", unit], timeout=30)
    time.sleep(3)
    _run(["rfkill", "unblock", "bluetooth"], timeout=10)
    btctl("power", "on")
    ok = controller_ok()
    if not ok:
        # stubborn wedge (field log: bluez restart alone leaves the kernel
        # looping 'hardware error 0x00' every 2s): power-cycle the radio
        # via rfkill around a second firmware re-attach
        log("==> Still down — radio power-cycle + second re-attach...")
        _run(["rfkill", "block", "bluetooth"], timeout=10)
        time.sleep(2)
        _run(["systemctl", "restart", "hciuart"], timeout=60)
        _run(["rfkill", "unblock", "bluetooth"], timeout=10)
        time.sleep(3)
        btctl("power", "on")
        ok = controller_ok()
    log("==> Controller is back." if ok
        else "==> Controller still down — a power cycle may be needed.")
    return ok


def discover(scan_secs=None):
    """Scan and return [{mac, name, audio}] for every device actually seen.
    'audio': False just means "could not confirm audio" — RSSI/UUID info is
    unreliable for unpaired devices."""
    bt_up()
    secs = scan_secs or SCAN_SECS
    log(f"Scanning {secs}s — put the speaker/headset in pairing mode now...")
    _c, out = btctl("--timeout", str(secs), "scan", "on", timeout=secs + 15)
    # Every device seen produces "[NEW] Device MAC ..." or "[CHG] Device ..."
    macs = sorted({m.group(1).upper() for m in re.finditer(
        r"Device ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", out)})
    found = []
    for mac in macs:
        _c, info = btctl("info", mac, timeout=10)
        m = re.search(r"^\s*Name: (.+)$", info, re.M)
        name = m.group(1).strip() if m else "(no name)"
        audio = bool(re.search(r"Icon: audio|Audio Sink|0000110b", info, re.I))
        found.append({"mac": mac, "name": name, "audio": audio})
    return found


def _print_devices(devices):
    for d in devices:
        log(f"  {d['mac']}  {d['name']}" + ("   [audio]" if d["audio"] else ""))


def _info_retry(mac):
    """bluetoothctl info can intermittently return nothing (D-Bus hiccup) —
    retry before concluding the device is unpaired."""
    for _ in range(3):
        _c, info = btctl("info", mac, timeout=10)
        if info.strip():
            return info
        time.sleep(1)
    return ""


def connect(mac):
    """Pair (if needed) + trust + A2DP connect + wait for the audio
    transport + route ALSA output. The full play.sh connect_headset flow."""
    bt_up()
    info = _info_retry(mac)

    if "Paired: yes" not in info:
        # Unknown/unpaired device: BlueZ must discover it before pairing works
        log(f"==> {mac} is not paired — scanning for it (pairing mode helps)...")
        btctl("--timeout", "12", "scan", "on", timeout=27)
        code, pair_out = btctl("pair", mac, timeout=45)
        log(pair_out.strip())
        if code != 0:
            if re.search("AlreadyExists", pair_out, re.I):
                # The bond exists after all (the info check lied) — continue
                log("==> Already paired — continuing.")
            elif re.search("AuthenticationFailed|AuthenticationCanceled",
                           pair_out, re.I):
                # Auth failure = the device holds a stale key. ONLY here is
                # it right to clear our bond and pair fresh.
                log("==> Stale key on the device — clearing bond and retrying once...")
                btctl("remove", mac, timeout=15)
                time.sleep(2)
                btctl("--timeout", "10", "scan", "on", timeout=25)
                code2, out2 = btctl("pair", mac, timeout=45)
                log(out2.strip())
                if code2 != 0:
                    return False
            elif re.search("not available", pair_out, re.I):
                log("Device not seen during scan. Is it powered on, close to "
                    "the Pi, and in pairing mode? Then retry the pairing.")
                return False
            else:
                return False
        btctl("trust", mac, timeout=15)

    # Always request a profile connect: right after pairing the device shows
    # "Connected: yes" from the pairing link itself, but A2DP is not up yet.
    log(f"==> Connecting (A2DP) to {mac}...")
    ok = False
    for _ in range(3):
        code, out = btctl("connect", mac, timeout=30)
        log(out.strip())
        if code == 0:
            ok = True
            break
        time.sleep(3)
    if not ok and _hci_crashed():
        # the connect attempt itself can crash the controller firmware
        # (A2DP + paging coexistence) — re-attach and try once more
        if recover():
            code, out = btctl("connect", mac, timeout=30)
            log(out.strip())
            ok = code == 0
    if not ok:
        log("Could not connect. If pairing keeps failing, try interactively:")
        log(f"  bluetoothctl  ->  scan on / pair {mac} / trust {mac} / connect {mac}")
        return False

    # The real "connected" test: bluealsa must expose an A2DP PCM
    log("==> Waiting for audio transport...")
    ready = False
    for _ in range(15):
        _c, pcm = _run(["bluealsa-aplay", "-L"], timeout=10)
        if mac.lower() in pcm.lower():
            ready = True
            break
        time.sleep(1)
    if ready:
        log("==> Audio transport ready.")
    else:
        log("WARNING: bluetooth connected, but no A2DP audio transport appeared.")
        log("Debug with: bluealsa-aplay -L   and   journalctl -u bluealsa -n 20")

    os.makedirs(os.path.dirname(MAC_FILE), exist_ok=True)
    with open(MAC_FILE, "w") as f:
        f.write(mac + "\n")
    _route_alsa(mac)
    _disconnect_others(mac)
    return True


def _disconnect_others(mac):
    """One output at a time. Headsets connect back on their own when
    powered on, so after switching, the old device would sit 'connected'
    next to the new one while audio follows only the configured device —
    confusing, and two live A2DP links strain the radio for nothing."""
    for line in _out(["devices", "Connected"]).splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[0] == "Device" \
                and parts[1].upper() != mac.upper():
            log(f"==> Disconnecting {parts[2] if len(parts) > 2 else parts[1]}"
                f" (one output at a time)")
            btctl("disconnect", parts[1], timeout=15)


def _route_alsa(mac):
    """Point the tapbox_bt ALSA device at this headset (tapbox_local for
    the HAT speaker is kept alongside)."""
    try:
        with open(ASOUND) as f:
            if mac in f.read():
                return
    except OSError:
        pass
    with open(ASOUND, "w") as f:
        f.write(f'''# Managed by tapbox (bt.py)
pcm.tapbox_bt {{
    type plug
    slave.pcm {{
        type bluealsa
        device "{mac}"
        profile "a2dp"
    }}
}}
# Built-in/HAT speaker (Pirate Audio / Amp SHIM, MAX98357A over I2S).
# Needs dtoverlay=hifiberry-dac (sudo tapbox-power hat-audio-on) + reboot.
pcm.tapbox_local {{
    type plug
    slave.pcm "hw:sndrpihifiberry"
}}
''')
    log(f"==> ALSA output routed to {mac}, restarting go-librespot...")
    _run(["systemctl", "restart", "go-librespot"], timeout=30)


def pair_auto(name_filter=None):
    """Find a device automatically (optionally filtered by name), connect."""
    seen = discover()
    if not seen:
        log("No bluetooth devices seen at all. Check that the device is in "
            "pairing mode and close to the Pi, then try again.")
        return False
    if name_filter:
        cands = [d for d in seen if name_filter.lower() in d["name"].lower()]
        if not cands:
            log(f"Nothing matching '{name_filter}'. Devices seen during scan:")
            _print_devices(seen)
            return False
    else:
        cands = [d for d in seen if d["audio"]]
        if not cands:
            log("Saw these devices, but none confirmed as audio (some speakers "
                "only advertise their audio profile after pairing):")
            _print_devices(seen)
            log('Pick yours by name: connect "<name>"')
            return False
    if len(cands) > 1:
        log("Multiple candidates found:")
        _print_devices(cands)
        log('Pick one by name: connect "<name>"')
        return False
    d = cands[0]
    log(f"==> Found device: {d['name']} ({d['mac']})")
    return connect(d["mac"])


def forget(mac):
    bt_up()  # a wedged controller made remove fail silently
    btctl("disconnect", mac, timeout=15)
    code, out = btctl("remove", mac, timeout=15)
    if code != 0 and "not available" not in out.lower():
        tail = out.strip().splitlines()[-1] if out.strip() else "unknown error"
        log(f"Could not remove {mac}: {tail}")
        return False
    try:
        active = open(MAC_FILE).read().strip()
    except OSError:
        active = ""
    if active == mac:
        os.remove(MAC_FILE)
        log("==> That was the active device — pair or pick another one.")
    log(f"==> Forgot {mac}")
    return True


def ensure():
    """Connect whatever we know about: remembered device, else auto-pair."""
    try:
        mac = open(MAC_FILE).read().strip()
    except OSError:
        mac = ""
    return connect(mac) if mac else pair_auto()




# --- daemon-facing API (tapboxd's /bt endpoints call these) ----------------------

BT_LOCK = threading.Lock()  # one pairing/connect operation at a time


def _out(args, timeout=10):
    """bluetoothctl stdout (stripped), '' on any failure."""
    _code, out = btctl(*args, timeout=timeout)
    return out


def bt_cli():
    """argv prefix for the BT helper subprocess. TAPBOX_PLAY injects a fake
    CLI in tests; otherwise this very file is executed as a script."""
    override = os.environ.get("TAPBOX_PLAY")
    if override:
        return ["bash", override]
    return [sys.executable, os.path.abspath(__file__)]


def bt_status():
    configured = None
    try:
        with open(MAC_FILE) as f:
            configured = f.read().strip() or None
    except OSError:
        pass
    devices = {}
    out = _out(["devices", "Paired"])
    if "Invalid" in out or "Unknown" in out:
        out = _out(["paired-devices"])  # older bluez
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device":
            devices[parts[1]] = {"mac": parts[1], "name": parts[2],
                                 "paired": True, "connected": False}
    for line in _out(["devices", "Connected"]).splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[0] == "Device" and parts[1] in devices:
            devices[parts[1]]["connected"] = True
    return {"configured": configured, "pairing": BT_LOCK.locked(),
            "devices": sorted(devices.values(),
                              key=lambda d: d["name"].lower())}


def bt_action(args, timeout):
    """Run a bt.py CLI command; None = another operation is in flight."""
    if not BT_LOCK.acquire(blocking=False):
        return None
    try:
        r = subprocess.run([*bt_cli(), *args], capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout + "\n" + r.stderr).strip()
        tail = "\n".join(out.splitlines()[-6:])
        log(f"bt {' '.join(args)} -> exit {r.returncode}")
        result = {"ok": r.returncode == 0, "output": tail}
    except (OSError, subprocess.TimeoutExpired) as e:
        result = {"ok": False, "output": f"failed: {e}"}
    finally:
        BT_LOCK.release()
    return {**result, **bt_status()}


def bt_scan():
    """~20s discovery; devices in pairing mode show up here. None = busy."""
    if not BT_LOCK.acquire(blocking=False):
        return None
    try:
        r = subprocess.run([*bt_cli(), "scan-raw"],
                           capture_output=True, text=True, timeout=60)
        found = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and MAC_RE.match(parts[0]):
                found.append({"mac": parts[0], "name": parts[1],
                              "audio": len(parts) > 2 and parts[2] == "yes"})
        # audio devices first, then by name
        found.sort(key=lambda d: (not d["audio"], d["name"].lower()))
        log(f"bt scan -> {len(found)} device(s)")
        return {"ok": True, "found": found}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "found": [], "output": f"failed: {e}"}
    finally:
        BT_LOCK.release()


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "scan":
        devices = discover()
        log("Devices seen during scan:")
        _print_devices(devices)
        return 0
    if cmd == "scan-raw":
        for d in discover():
            print(f"{d['mac']}\t{d['name']}\t{'yes' if d['audio'] else 'no'}")
        return 0
    if cmd == "connect":
        return 0 if pair_auto(args[1] if len(args) > 1 else None) else 1
    if cmd in ("use", "forget", "disconnect"):
        if len(args) < 2 or not MAC_RE.match(args[1]):
            print(f"usage: bt.py {cmd} <MAC>", file=sys.stderr)
            return 1
        if cmd == "disconnect":
            code, out = btctl("disconnect", args[1], timeout=15)
            log(out.strip().splitlines()[-1] if out.strip() else "")
            return 0 if code == 0 else 1
        fn = connect if cmd == "use" else forget
        return 0 if fn(args[1]) else 1
    if cmd == "ensure":
        return 0 if ensure() else 1
    if cmd == "recover":
        return 0 if recover() else 1
    print(__doc__.split("CLI", 1)[1], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
