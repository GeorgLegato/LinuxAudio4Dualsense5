#!/usr/bin/python3
# ds5_keymap.py — DualSense -> keyboard, reading /dev/hidraw DIRECTLY.
#
#   left stick  -> WASD            (held while deflected — movement)
#   right stick -> arrow keys      (PULSED — camera, see below)
#
# Why hidraw and not evdev: over Bluetooth the controller sends an audio-
# feedback stream under the SAME HID report id (0x31) as gamepad input. The
# kernel (and Steam) can't tell them apart, so the evdev stick gets poisoned
# (jitters 0..255) whenever BT audio runs. By reading hidraw ourselves we see
# the raw 0x31 reports and DROP the feedback frames (0xd4 marker).
#
# Why PULSE the right stick: Steam (and a plain "hold the key" mapper) HOLD the
# arrow key down while the stick is deflected, so the game pans the camera at
# its own full internal speed — far too fast, uncontrollable. On a keyboard you
# instead TAP the arrow key briefly to nudge the camera. This mapper reproduces
# that: it emits short key taps whose RATE scales with how far you push the
# stick. Barely pushed = a few taps/s (slow). Fully pushed = many taps/s (fast).
# Result: smooth, controllable camera even in a game that was never meant for a
# gamepad. Tune with --cam-min / --cam-max / --tap-ms.
#
# Disable "PlayStation Configuration Support" in Steam so it doesn't also grab
# the controller. /dev/uinput must be writable (usually is, via uaccess).
#
#   ./ds5_keymap.py [--deadzone N] [--right-deadzone N]
#                   [--cam-min HZ] [--cam-max HZ] [--tap-ms MS] [--debug]

import sys, time, glob, os
from evdev import UInput, ecodes as e

DEADZONE   = 50        # left-stick travel from center before WASD triggers
R_DEADZONE = 40        # right-stick travel from center before camera triggers
CENTER     = 128
CAM_MIN_HZ = 3.0       # taps/s at the edge of the right-stick deadzone (slow)
CAM_MAX_HZ = 22.0      # taps/s at full right-stick deflection (fast)
TAP_MS     = 12        # how long each camera key tap is held down

# held while deflected (movement)
WASD = {'up': e.KEY_W, 'down': e.KEY_S, 'left': e.KEY_A, 'right': e.KEY_D}
# pulsed (camera)
ARROW = {'up': e.KEY_UP, 'down': e.KEY_DOWN, 'left': e.KEY_LEFT, 'right': e.KEY_RIGHT}

# Raw BT input report 0x31 over hidraw (a1 prefix already stripped by kernel):
#   [0]=0x31 [1]=seq [2]=LX [3]=LY [4]=RX [5]=RY ...
# Audio-feedback frame (same id!):  [3]=0xd4 [4]=0xff [5]=0xfe ...
LX, LY, RX, RY = 2, 3, 4, 5


def find_hidraw():
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            if "HID_ID=0005:0000054C:00000CE6" in open(d + "/device/uevent").read():
                return "/dev/" + os.path.basename(d)
        except OSError:
            pass
    return None


def is_feedback(r):
    # audio-feedback marker — drop these, they are NOT gamepad state
    return len(r) > 5 and r[3] == 0xd4 and r[4] == 0xff and r[5] == 0xfe


def arg(name, default, cast):
    if name in sys.argv:
        return cast(sys.argv[sys.argv.index(name) + 1])
    return default


def main():
    dz   = arg("--deadzone", DEADZONE, int)
    rdz  = arg("--right-deadzone", R_DEADZONE, int)
    cmin = arg("--cam-min", CAM_MIN_HZ, float)
    cmax = arg("--cam-max", CAM_MAX_HZ, float)
    tap  = arg("--tap-ms", TAP_MS, float) / 1000.0
    debug = "--debug" in sys.argv

    path = find_hidraw()
    if not path:
        print("DualSense (BT) hidraw not found.", file=sys.stderr); return 1
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    print(f"hidraw: {path}", file=sys.stderr)
    print(f"  left stick -> WASD (deadzone {dz})", file=sys.stderr)
    print(f"  right stick -> arrows, pulsed {cmin:g}..{cmax:g} taps/s "
          f"(deadzone {rdz}, tap {tap*1000:g}ms)", file=sys.stderr)
    print("  Ctrl+C to stop.", file=sys.stderr)

    ui = UInput({e.EV_KEY: list(WASD.values()) + list(ARROW.values())},
                name="ds5-keymap")

    held = {k: False for k in WASD}          # left stick: held state
    down = {k: False for k in ARROW}         # right stick: currently in a tap
    next_tap = {k: 0.0 for k in ARROW}       # right stick: when next tap may fire
    release_at = {k: 0.0 for k in ARROW}     # right stick: when current tap ends

    def set_held(d, on):
        if held[d] != on:
            held[d] = on
            ui.write(e.EV_KEY, WASD[d], 1 if on else 0); ui.syn()

    def tap_key(d, on):
        ui.write(e.EV_KEY, ARROW[d], 1 if on else 0); ui.syn()

    # latest stick values (updated as reports arrive; scheduler runs every tick)
    lx = ly = rx = ry = CENTER
    used = dropped = 0
    last = time.monotonic()

    try:
        while True:
            # drain all pending reports so we always act on the freshest stick state
            got = False
            while True:
                try:
                    r = os.read(fd, 128)
                except BlockingIOError:
                    break
                if not r or r[0] != 0x31:
                    continue
                if is_feedback(r):
                    dropped += 1
                    continue                 # <-- ignore audio frames
                used += 1
                lx, ly, rx, ry = r[LX], r[LY], r[RX], r[RY]
                got = True

            now = time.monotonic()

            # ---- left stick: held WASD (movement) ----
            set_held('left',  lx < CENTER - dz)
            set_held('right', lx > CENTER + dz)
            set_held('up',    ly < CENTER - dz)
            set_held('down',  ly > CENTER + dz)

            # ---- right stick: pulsed arrows (camera), rate ∝ deflection ----
            axes = (('left', CENTER - rx), ('right', rx - CENTER),
                    ('up', CENTER - ry), ('down', ry - CENTER))
            for d, defl in axes:
                if defl <= rdz:
                    if down[d]:
                        tap_key(d, 0); down[d] = False
                    next_tap[d] = 0.0
                    continue
                # end an in-progress tap once its hold time elapsed
                if down[d] and now >= release_at[d]:
                    tap_key(d, 0); down[d] = False
                # fire a fresh tap when scheduled
                if not down[d] and now >= next_tap[d]:
                    frac = min(1.0, (defl - rdz) / float(127 - rdz))
                    rate = cmin + (cmax - cmin) * frac
                    tap_key(d, 1); down[d] = True
                    release_at[d] = now + tap
                    next_tap[d] = now + 1.0 / rate

            if debug and now - last >= 1.0:
                print(f"[dbg] gamepad={used}/s feedback-dropped={dropped}/s "
                      f"L=({lx},{ly}) R=({rx},{ry})", file=sys.stderr)
                used = dropped = 0; last = now

            if not got:
                time.sleep(0.002)            # idle tick so pulse timing stays smooth
    except KeyboardInterrupt:
        pass
    finally:
        for k in WASD:
            set_held(k, False)
        for k in ARROW:
            if down[k]:
                tap_key(k, 0)
        ui.close(); os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
