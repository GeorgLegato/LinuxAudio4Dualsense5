#!/usr/bin/env python3
"""
Langsamer Sweep über plausible Speaker-On-Bytes mit großer Anzeige.
User kann beim Hören mitlesen welcher Wert aktuell läuft.
"""
import sys
import time
import subprocess
from pydualsense import pydualsense
from pydualsense.enums import ConnectionType


# Bit-Klassen mit hoher Wahrscheinlichkeit Speaker-bit zu treffen
CANDIDATES = [
    0x40, 0x44, 0x48, 0x4C,    # bit 6 set
    0x50, 0x54,                 # bit 4|6
    0x60, 0x64, 0x68,           # bit 5|6
    0x70, 0x74,                 # bit 4|5|6
    0x80, 0x84,                 # bit 7
    0xC0, 0xC4,                 # bit 6|7
    0xE0, 0xF0,                 # high bits
]


def make_patcher(byte8_value, orig):
    def patched():
        rep = orig()
        if rep and rep[0] == 0x02:
            rep[2] = (rep[2] | 0x04) & ~0x02
            rep[5] = 0x7F
            rep[6] = 0x7F   # speaker volume max
            rep[7] = 0x40
            rep[8] = byte8_value
        return rep
    return patched


def main():
    ds = pydualsense()
    ds.init()
    if ds.conType != ConnectionType.USB:
        print("Need USB connection")
        ds.close()
        return 1

    orig = ds.prepareReport
    for val in CANDIDATES:
        ds.prepareReport = make_patcher(val, orig)
        print("\n" + "=" * 60, flush=True)
        print(f"   Byte[8] = 0x{val:02X}        ({val:3d}   bin={val:08b})", flush=True)
        print("=" * 60, flush=True)
        time.sleep(0.5)  # User kann Wert lesen
        print(f"   ... spiele 4s Sinus", flush=True)
        subprocess.run(["paplay", "/tmp/sine600.wav"], capture_output=True, timeout=6)
        time.sleep(0.8)

    ds.prepareReport = orig
    ds.close()
    print("\nFertig.", flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
