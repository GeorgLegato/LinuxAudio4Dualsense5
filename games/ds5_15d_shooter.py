#!/usr/bin/python3
# ds5_15d_shooter.py — a tiny "1.5D" shooter played on the DualSense LEDs, over
# Bluetooth. The 5 white player LEDs (below the touchpad) are a 5-cell lane:
# enemies march in from the right and creep toward you on the left. Press CROSS
# (X) on the controller — or SPACE on the keyboard — to shoot the nearest one
# before it slips past. The RGB lightbar shows the danger (calm teal far away ->
# red up close), flashes white on a kill and red (with a rumble) when you're hit.
#
# All over plain Bluetooth: LEDs/rumble via the dedicated 0x31 output report,
# buttons read straight from /dev/hidraw. It stops the audio service while it
# runs (sole sender) and restarts it on exit.
#
#   ./ds5_15d_shooter.py [--flip]      # --flip if the LED lane runs the wrong way

import os, sys, time, glob, zlib, struct, threading, random, select, curses

DS5_UEVENT = "0005:0000054C:00000CE6"
LANE = 5                         # 5 player LEDs


def find_hidraw():
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            if DS5_UEVENT in open(d + "/device/uevent").read():
                return "/dev/" + os.path.basename(d)
        except OSError:
            pass
    return None


def crc(d):
    return zlib.crc32(bytes([0xA2]) + d) & 0xFFFFFFFF


def led_report(seq, r, g, b, player, bright=0x02, motor=0):
    # Dedicated 0x31 BT output report: lightbar RGB + player LEDs (+ rumble).
    p = bytearray(78)
    p[0] = 0x31; p[1] = (seq << 4) & 0xF0; p[2] = 0x10
    p[3] = 0x03 if motor else 0x00        # valid_flag0: rumble enable (best effort)
    p[4] = 0x04 | 0x10                    # valid_flag1: lightbar + player LEDs
    p[5] = motor & 0xFF; p[6] = motor & 0xFF    # motor_right / motor_left
    p[45] = bright                        # led_brightness (player LEDs)
    p[46] = player & 0x1F                 # player_leds bitmask
    p[47] = r & 0xFF; p[48] = g & 0xFF; p[49] = b & 0xFF
    struct.pack_into("<I", p, 74, crc(bytes(p[:74])))
    return bytes(p)


class Pad:
    """Reads /dev/hidraw and keeps the latest face-button bytes."""
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.btn = bytes(4)               # report bytes [9..12]
        self.stop = False

    def run(self):
        while not self.stop:
            r, _, _ = select.select([self.fd], [], [], 0.03)
            if not r:
                continue
            try:
                while True:
                    b = os.read(self.fd, 128)
                    if not b or b[0] != 0x31:
                        continue
                    if len(b) > 5 and b[3] == 0xd4:   # mic-feedback frame
                        continue
                    if len(b) >= 13:
                        self.btn = bytes(b[9:13])
            except BlockingIOError:
                pass
        os.close(self.fd)

    def pressed(self, idx, mask):
        return bool(self.btn[idx] & mask) if idx < len(self.btn) else False


class Renderer:
    """Sends the LED/rumble report ~20x/s from shared state (keeps it asserted)."""
    def __init__(self, path):
        self.fd = os.open(path, os.O_WRONLY)
        self.rgb = (0, 80, 80); self.mask = 0; self.motor = 0
        self.seq = 0; self.stop = False

    def run(self):
        while not self.stop:
            r, g, b = self.rgb
            try:
                os.write(self.fd, led_report(self.seq, r, g, b, self.mask, motor=self.motor))
            except OSError:
                break
            self.seq = (self.seq + 1) & 0x0F
            time.sleep(0.05)
        try:
            os.write(self.fd, led_report(0, 0, 0, 0, 0))   # LEDs aus beim Ende
        except OSError:
            pass
        os.close(self.fd)


def lane_mask(enemies, flip):
    m = 0
    for p in enemies:
        if 0 <= p < LANE:
            bit = (LANE - 1 - p) if flip else p
            m |= (1 << bit)
    return m


def danger_color(nearest):
    d = max(0.0, min(1.0, (LANE - nearest) / float(LANE)))   # 0 far .. 1 close
    return int(255 * d), int(120 * (1 - d)), int(40 + 80 * (1 - d))


def main(scr):
    flip = "--flip" in sys.argv
    path = find_hidraw()
    if not path:
        scr.addstr(0, 0, "DualSense (BT) nicht gefunden."); scr.getch(); return

    os.system("systemctl --user stop ds5-membrane-sink.service 2>/dev/null")
    os.system("pkill -f ds5_membrane_sink 2>/dev/null")
    time.sleep(0.4)

    pad = Pad(path); ren = Renderer(path)
    threading.Thread(target=pad.run, daemon=True).start()
    threading.Thread(target=ren.run, daemon=True).start()
    curses.curs_set(0); scr.nodelay(True); scr.timeout(20)

    def msg(y, s, a=0):
        try: scr.addstr(y, 2, s, a)
        except curses.error: pass

    # --- Start / Auto-Kalibrierung des Feuer-Buttons ---
    time.sleep(0.3)
    base = pad.btn
    fire_idx = fire_mask = None
    key_fire = False
    while True:
        scr.erase()
        msg(0, "DS5  1.5D-SHOOTER", curses.A_BOLD)
        msg(2, "Druecke  X (Cross)  am Controller  — oder LEERTASTE — zum Start.")
        msg(3, "Ziel: schiess die Gegner ab, bevor sie links rauslaufen.", curses.A_DIM)
        msg(4, "[q] quit", curses.A_DIM)
        scr.refresh()
        ch = scr.getch()
        if ch in (ord('q'), 27):
            pad.stop = ren.stop = True; time.sleep(0.1); return
        if ch == ord(' '):
            key_fire = True; break
        cur = pad.btn
        hit = None
        for i in range(len(cur)):
            diff = cur[i] & ~base[i]            # neu gesetzte Bits
            if diff:
                hit = (i, diff & (-diff))       # niedrigstes gesetztes Bit
                break
        if hit:
            fire_idx, fire_mask = hit
            break

    # --- Spiel ---
    while True:
        enemies = []; score = 0; lives = 3
        tick = 0.30; last = time.monotonic(); spawn_t = time.monotonic()
        flash = (0, 0, 0); flash_until = 0.0; motor = 0; motor_until = 0.0
        fire_prev = False
        while lives > 0:
            now = time.monotonic()
            ch = scr.getch()
            if ch in (ord('q'), 27):
                pad.stop = ren.stop = True; time.sleep(0.1); return
            # Feuer-Erkennung: Tastatur-Leertaste ODER Controller-X-Flanke
            ctrl = pad.pressed(fire_idx, fire_mask) if fire_idx is not None else False
            fire = (ch == ord(' ')) or (ctrl and not fire_prev)
            fire_prev = ctrl
            if fire and enemies:
                enemies.pop(0)                  # naechsten (vordersten) Gegner treffen
                score += 1
                flash = (255, 255, 255); flash_until = now + 0.10

            # Tick: Gegner ruecken vor, Spawns, Schwierigkeit
            if now - last >= tick:
                last = now
                enemies = [p - 1 for p in enemies]
                while enemies and enemies[0] < 0:     # links rausgelaufen -> Treffer
                    enemies.pop(0); lives -= 1
                    flash = (255, 0, 0); flash_until = now + 0.25
                    motor = 180; motor_until = now + 0.20
                # Spawn rechts (Zelle LANE-1), Rate steigt mit Score
                if now - spawn_t > max(0.35, 1.1 - score * 0.03):
                    spawn_t = now
                    if not enemies or enemies[-1] < LANE - 1:
                        enemies.append(LANE - 1)
                tick = max(0.10, 0.30 - score * 0.004)

            if now > motor_until: motor = 0
            base_col = danger_color(min(enemies) if enemies else LANE)
            col = flash if now < flash_until else base_col
            ren.rgb = col; ren.mask = lane_mask(enemies, flip); ren.motor = motor

            # Terminal-Spiegel der Bahn
            cells = ["X" if p in enemies else "." for p in range(LANE)]
            if flip: cells = cells[::-1]
            scr.erase()
            msg(0, "DS5  1.5D-SHOOTER", curses.A_BOLD)
            msg(2, "Gun >| " + " ".join(cells) + " |")
            msg(4, "Score: %3d     Leben: %s" % (score, "<3 " * lives))
            msg(5, "FIRE = X / Leertaste     [q] quit",
                curses.A_BOLD if now < flash_until and col == (255, 255, 255) else curses.A_DIM)
            scr.refresh()
            time.sleep(0.012)

        # Game over
        ren.rgb = (120, 0, 0); ren.mask = 0x1F; ren.motor = 120
        for _ in range(6):
            scr.erase()
            msg(0, "GAME OVER", curses.A_BOLD)
            msg(2, "Score: %d" % score)
            msg(4, "[X / Leertaste] nochmal     [q] quit", curses.A_DIM)
            scr.refresh(); time.sleep(0.12)
        ren.motor = 0
        # auf Neustart/Quit warten
        while True:
            ch = scr.getch()
            if ch in (ord('q'), 27):
                pad.stop = ren.stop = True; time.sleep(0.1); return
            if ch == ord(' ') or (fire_idx is not None and pad.pressed(fire_idx, fire_mask)):
                break
            time.sleep(0.02)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    finally:
        os.system("systemctl --user start ds5-membrane-sink.service 2>/dev/null")
        print("Audio-Service wieder gestartet.")
