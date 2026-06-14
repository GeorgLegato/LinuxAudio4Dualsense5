#!/usr/bin/python3
# ds5_keymap.py — DualSense left stick -> WASD, reading /dev/hidraw DIRECTLY.
#
# Why hidraw and not evdev: over Bluetooth the controller sends an audio-
# feedback stream under the SAME HID report id (0x31) as gamepad input. The
# kernel (and Steam) can't tell them apart, so the evdev stick gets poisoned
# (jitters 0..255) whenever BT audio runs. By reading hidraw ourselves we see
# the raw 0x31 reports and can DROP the feedback frames (0xd4 marker), keeping
# only real gamepad reports. -> stick->WASD works even while BT audio plays.
#
# Disable "PlayStation Configuration Support" in Steam so it doesn't also grab
# the controller. /dev/uinput must be writable (usually is, via uaccess).
#
#   ./ds5_keymap.py [--deadzone N] [--debug]

import sys, time, glob, os
from evdev import UInput, ecodes as e

DEADZONE = 50          # stick travel from center before a key triggers
CENTER   = 128
KEYMAP   = {'up': e.KEY_W, 'down': e.KEY_S, 'left': e.KEY_A, 'right': e.KEY_D}

# Raw BT input report 0x31 over hidraw (a1 prefix already stripped by kernel):
#   [0]=0x31 [1]=seq [2]=LX [3]=LY [4]=RX [5]=RY ...
# Audio-feedback frame (same id!):  [3]=0xd4 [4]=0xff [5]=0xfe ...
LX, LY = 2, 3

def find_hidraw():
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            if "HID_ID=0005:0000054C:00000CE6" in open(d+"/device/uevent").read():
                return "/dev/"+os.path.basename(d)
        except OSError:
            pass
    return None

def is_feedback(r):
    # audio-feedback marker — drop these, they are NOT gamepad state
    return len(r) > 5 and r[3] == 0xd4 and r[4] == 0xff and r[5] == 0xfe

def main():
    dz = DEADZONE
    debug = "--debug" in sys.argv
    if "--deadzone" in sys.argv:
        dz = int(sys.argv[sys.argv.index("--deadzone")+1])

    path = find_hidraw()
    if not path:
        print("DualSense (BT) hidraw not found.", file=sys.stderr); return 1
    fd = os.open(path, os.O_RDONLY)
    print(f"hidraw: {path}   stick->WASD (deadzone {dz}), Ctrl+C to stop.",
          file=sys.stderr)

    ui = UInput({e.EV_KEY: list(KEYMAP.values())}, name="ds5-keymap")
    state = {k: False for k in KEYMAP}
    dropped = 0; used = 0; last = time.monotonic()

    def setk(d, on):
        if state[d] != on:
            state[d] = on
            ui.write(e.EV_KEY, KEYMAP[d], 1 if on else 0); ui.syn()

    try:
        while True:
            r = os.read(fd, 128)
            if not r or r[0] != 0x31:
                continue
            if is_feedback(r):
                dropped += 1
                continue                      # <-- ignore audio frames
            used += 1
            lx, ly = r[LX], r[LY]
            setk('left',  lx < CENTER - dz)
            setk('right', lx > CENTER + dz)
            setk('up',    ly < CENTER - dz)
            setk('down',  ly > CENTER + dz)
            if debug and time.monotonic() - last >= 1.0:
                print(f"[dbg] gamepad={used}/s feedback-dropped={dropped}/s "
                      f"LX={lx} LY={ly}", file=sys.stderr)
                used = dropped = 0; last = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        for k in KEYMAP: setk(k, False)
        ui.close(); os.close(fd)
    return 0

if __name__ == "__main__":
    sys.exit(main())
