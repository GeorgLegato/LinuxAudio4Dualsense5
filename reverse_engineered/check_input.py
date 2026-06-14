#!/usr/bin/env python3
# check_input.py — zeigt, was der DualSense ueber hidraw sendet.
#
# Unterscheidet sauberen Gamepad-Input von dem Audio-Duplex-Feedback-Stream
# (Report 0x31 mit 0xd4-Marker), der nach BT-Audio-Nutzung "haengen" bleibt und
# von Steam als Phantom-Stick-Input fehlinterpretiert wird.
#
#   ./check_input.py            # 2s messen, Verdikt ausgeben
#   ./check_input.py 5          # 5s messen

import os, sys, time, glob

def find_hidraw():
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            if "HID_ID=0005:0000054C:00000CE6" in open(f"{d}/device/uevent").read():
                return f"/dev/{os.path.basename(d)}"
        except OSError:
            pass
    return None

def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    path = find_hidraw()
    if not path:
        print("DualSense (BT) nicht gefunden."); return 1
    print(f"Lese {path} fuer {secs:.0f}s ...")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except PermissionError:
        print("Kein Lesezugriff — mit sudo versuchen."); return 1
    d4 = gp = 0
    ex_d4 = ex_gp = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        try:
            r = os.read(fd, 128)
        except BlockingIOError:
            time.sleep(0.001); continue
        if not r or r[0] != 0x31:
            continue
        if len(r) > 3 and r[3] == 0xd4:        # Audio-Feedback-Marker
            d4 += 1; ex_d4 = ex_d4 or r[:8].hex(' ')
        else:
            gp += 1; ex_gp = ex_gp or r[:8].hex(' ')
    os.close(fd)
    print(f"  Gamepad-Reports : {gp:5d}  ({gp/secs:.0f}/s)   {ex_gp or '-'}")
    print(f"  Audio-Feedback  : {d4:5d}  ({d4/secs:.0f}/s)   {ex_d4 or '-'}")
    print()
    if d4 > 0:
        print("==> DUPLEX HAENGT: der Audio-Feedback-Stream laeuft.")
        print("    Steam liest die 0xd4-Bytes als Stick-Auslenkung (Phantom-Input).")
        print("    Fix: Controller trennen + neu verbinden (PS-Button) oder Hard-Reset.")
    else:
        print("==> SAUBER: nur Gamepad-Input, kein Audio-Feedback. Steam ist happy.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
