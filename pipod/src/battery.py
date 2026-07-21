#!/usr/bin/env python3
"""pipod battery reader — DRAFT (untested).

Reads a MAX17048 LiPo fuel gauge over I2C (addr 0x36) and writes battery
voltage + state-of-charge to a small JSON file that podui.py reads for its
battery indicator. This is the ~30-line shim that replaces PiSugar's
built-in I2C status when using the bare-LiPo power path (RESEARCH §2).

MAX17048 registers: VCELL=0x02 (78.125 uV/LSB across 16 bits >>4),
SOC=0x04 (1/256 % per LSB). Both big-endian.
Datasheet: https://www.analog.com/media/en/technical-documentation/data-sheets/MAX17048-MAX17049.pdf

Run as pipod-battery.service; podui reads STATE_FILE (no coupling).
"""

import json
import os
import time

I2C_BUS = int(os.environ.get("PIPOD_I2C_BUS", "1"))
ADDR = 0x36
REG_VCELL = 0x02
REG_SOC = 0x04
STATE_FILE = os.environ.get("PIPOD_BATTERY", "/run/pipod-battery.json")
PERIOD_S = float(os.environ.get("PIPOD_BATTERY_PERIOD", "30"))


def log(msg):
    print(f"battery: {msg}", flush=True)


def _read_word(bus, reg):
    # MAX17048 is big-endian; smbus read_word_data is little-endian -> swap
    raw = bus.read_word_data(ADDR, reg)
    return ((raw & 0xFF) << 8) | (raw >> 8)


def main():
    try:
        from smbus2 import SMBus
    except ImportError:
        log("smbus2 missing: pip install smbus2")
        return
    while True:
        try:
            with SMBus(I2C_BUS) as bus:
                vcell = _read_word(bus, REG_VCELL)
                soc = _read_word(bus, REG_SOC)
            volts = (vcell >> 4) * 78.125e-6
            percent = min(100.0, soc / 256.0)
            data = {"percent": round(percent, 1), "volts": round(volts, 3),
                    "ts": int(time.time())}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            log(f"read failed: {e}")   # gauge absent / bus busy — skip tick
        time.sleep(PERIOD_S)


if __name__ == "__main__":
    main()
