#!/usr/bin/env python3
"""Fake org.bluez + org.bluealsa for testing the dbus backend without
hardware (PLAN-bt-dbus.md §8). Runs on a PRIVATE bus:

    dbus-daemon --session --print-address --fork   (or bt_parity.py
    starts one for you) — export the address as DBUS_SYSTEM_BUS_ADDRESS
    so btbus's SystemBus() lands here instead of the real system bus.

Exports:
  /org/bluez            ObjectManager over the fake device tree
  /org/bluez/hci0       org.bluez.Adapter1 (Powered/Pairable props,
                        Start/StopDiscovery, SetDiscoveryFilter,
                        RemoveDevice)
  /org/bluez/hci0/dev_* org.bluez.Device1 (Properties incl. RSSI while
                        'discovering')
  /org/bluealsa         ObjectManager exposing org.bluealsa.PCM1 objects
  /org/tapbox/mock      control interface (org.tapbox.Mock):
                        AddDevice(mac, name, paired, connected, rssi)
                        SetConnected(mac, bool)  SetPcm(mac, bool)
                        DropDevice(mac)

Requires python3-dbus + python3-gi (present on the rig, apt on dev
machines). Deliberately minimal: grow it with phase B (Pair errors via
SetPairResult, Agent1 callbacks) per the plan's test strategy.
"""

import os
import sys

import dbus
import dbus.bus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

DBusGMainLoop(set_as_default=True)
_ADDR = os.environ.get("DBUS_SYSTEM_BUS_ADDRESS")
# explicit connection: never risk landing on the real system bus
BUS = dbus.bus.BusConnection(_ADDR) if _ADDR else dbus.SystemBus()

DEVICES = {}   # mac -> {name, paired, connected, rssi}
PCMS = {}      # mac -> bool
DISCOVERING = [False]


def dev_path(mac):
    return "/org/bluez/hci0/dev_" + mac.upper().replace(":", "_")


def device_props(mac):
    d = DEVICES[mac]
    props = {
        "Address": mac.upper(),
        "Alias": d["name"],
        "Paired": dbus.Boolean(d["paired"]),
        "Connected": dbus.Boolean(d["connected"]),
        "Trusted": dbus.Boolean(d.get("trusted", False)),
        "UUIDs": dbus.Array(d.get("uuids", []), signature="s"),
    }
    if DISCOVERING[0] and d.get("rssi") is not None:
        props["RSSI"] = dbus.Int16(d["rssi"])
    return props


class BluezRoot(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.ObjectManager",
                         out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objs = {"/org/bluez/hci0": {"org.bluez.Adapter1": {
            "Powered": dbus.Boolean(True),
            "Pairable": dbus.Boolean(True)}}}
        for mac in DEVICES:
            objs[dev_path(mac)] = {"org.bluez.Device1": device_props(mac)}
        return objs


class Adapter(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return dbus.Boolean(True)  # Powered / Pairable

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ssv")
    def Set(self, iface, prop, value):
        pass

    @dbus.service.method("org.bluez.Adapter1", in_signature="a{sv}")
    def SetDiscoveryFilter(self, filt):
        pass

    @dbus.service.method("org.bluez.Adapter1")
    def StartDiscovery(self):
        DISCOVERING[0] = True

    @dbus.service.method("org.bluez.Adapter1")
    def StopDiscovery(self):
        DISCOVERING[0] = False

    @dbus.service.method("org.bluez.Adapter1", in_signature="o")
    def RemoveDevice(self, path):
        for mac in list(DEVICES):
            if dev_path(mac) == str(path):
                del DEVICES[mac]


class Device(dbus.service.Object):
    def __init__(self, mac):
        self.mac = mac
        super().__init__(BUS, dev_path(mac))

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return device_props(self.mac)


class BluealsaRoot(dbus.service.Object):
    @dbus.service.method("org.freedesktop.DBus.ObjectManager",
                         out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        objs = {}
        for mac, present in PCMS.items():
            if present:
                path = ("/org/bluealsa/hci0/dev_"
                        + mac.upper().replace(":", "_") + "/a2dpsrc/sink")
                objs[path] = {"org.bluealsa.PCM1": {
                    "Device": dbus.ObjectPath(dev_path(mac)),
                    "Mode": "sink", "Transport": "A2DP-source"}}
        return objs


class Mock(dbus.service.Object):
    @dbus.service.method("org.tapbox.Mock", in_signature="ssbbn")
    def AddDevice(self, mac, name, paired, connected, rssi):
        mac = str(mac).upper()
        DEVICES[mac] = {"name": str(name), "paired": bool(paired),
                        "connected": bool(connected),
                        "rssi": int(rssi) if int(rssi) != 0 else None}
        Device(mac)

    @dbus.service.method("org.tapbox.Mock", in_signature="sb")
    def SetConnected(self, mac, connected):
        DEVICES[str(mac).upper()]["connected"] = bool(connected)

    @dbus.service.method("org.tapbox.Mock", in_signature="sb")
    def SetPcm(self, mac, present):
        PCMS[str(mac).upper()] = bool(present)

    @dbus.service.method("org.tapbox.Mock", in_signature="s")
    def DropDevice(self, mac):
        DEVICES.pop(str(mac).upper(), None)


def main():
    for name in ("org.bluez", "org.bluealsa"):
        dbus.service.BusName(name, BUS)
    BluezRoot(BUS, "/")  # real bluez exports ObjectManager at the root
    Adapter(BUS, "/org/bluez/hci0")
    BluealsaRoot(BUS, "/org/bluealsa")
    Mock(BUS, "/org/tapbox/mock")
    print("fake-bluezd ready", flush=True)
    GLib.MainLoop().run()


if __name__ == "__main__":
    sys.exit(main())
