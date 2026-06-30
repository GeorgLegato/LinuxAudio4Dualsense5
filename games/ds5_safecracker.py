#!/usr/bin/python3
# ds5_safecracker.py — a haptic lock-picking game felt entirely through the
# DualSense, over Bluetooth. Pick a pin-tumbler lock by FEEL:
#
#   R2 trigger  = hold tension (you feel the adaptive-trigger resistance)
#   left stick UP = raise the current pin
#   rumble swells as you near a pin's shear line ("hot / cold"), a metallic
#     CLICK marks the spot; hold there briefly -> CLUNK, the pin sets, LED blips
#     green, on to the next pin.
#   push too high -> OVERSET: the pin drops, a hard jolt, and the alarm jumps.
#   the lightbar is the room alarm: calm green -> red over time and on mistakes.
#   set all pins before it hits red = cracked. Eyes closed, just your hands.
#
# Uses: 0x31 output report for LEDs + adaptive trigger + rumble; 0x36/Opus for
# the metallic sounds; left stick + R2 read from /dev/hidraw. Stops the audio
# service while it runs and restarts it on exit.
#
#   ./ds5_safecracker.py [--pins N]

import os, sys, time, glob, zlib, struct, math, threading, random, select, curses

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "reverse_engineered"))
import ps5bt_membrane as D


def crc(b): return zlib.crc32(bytes([0xA2]) + b) & 0xFFFFFFFF

def feedback_params(start, strength):              # adaptive-trigger FEEDBACK
    fv = (max(1, min(8, strength)) - 1) & 0x07
    active = force = 0
    for z in range(start, 10):
        active |= (1 << z); force |= (fv << (3 * z))
    return [active & 0xff, (active >> 8) & 0xff,
            force & 0xff, (force >> 8) & 0xff, (force >> 16) & 0xff, (force >> 24) & 0xff,
            0, 0, 0, 0]

def out_report(seq, r, g, b, motor, trig_str):
    # one 0x31: lightbar (valid_flag1) + rumble + R2 FEEDBACK (valid_flag0)
    p = bytearray(78); p[0] = 0x31; p[1] = (seq << 4) & 0xF0; p[2] = 0x10
    p[3] = 0x01 | 0x04                             # valid_flag0: rumble + right trigger
    p[4] = 0x04                                    # valid_flag1: lightbar
    p[5] = motor & 0xFF; p[6] = motor & 0xFF       # motor_right / motor_left
    if trig_str > 0:                               # R2 = tension resistance
        pr = feedback_params(2, trig_str); p[13] = 0x21
        for i in range(10): p[14 + i] = pr[i]
    else:
        p[13] = 0x05
    p[47] = r & 0xFF; p[48] = g & 0xFF; p[49] = b & 0xFF
    struct.pack_into("<I", p, 74, crc(bytes(p[:74])))
    return bytes(p)


def make_sound(enc, segments):
    # segments: list of (freq, ms, amp, glide, noise) -> one list of opus frames
    pcm = bytearray(); ph = 0.0
    for (freq, ms, amp, glide, noise) in segments:
        n = int(ms * 48); f = freq; env = 1.0
        for _ in range(n):
            v = amp * env * ((1 - noise) * math.sin(ph) + noise * (random.random() * 2 - 1))
            ph += 2 * math.pi * f / 48000; f *= glide; env *= 0.99965
            s = int(max(-1, min(1, v)) * 16000); pcm += struct.pack("<hh", s, s)
    while len(pcm) % (480 * 4): pcm += b"\x00\x00\x00\x00"
    frames = []
    for o in range(0, len(pcm), 480 * 4):
        frames.append(enc.encode(bytes(pcm[o:o + 480 * 4]), 480))
    return frames


class G:
    rgb = (0, 60, 0); motor = 0; trig = 4
    sound = []; silence = None; lock = threading.Lock(); stop = False

def play(frames):
    with G.lock: G.sound = list(frames) + ([G.silence] if G.silence else [])

def sender(path):
    fd = os.open(path, os.O_WRONLY); os.write(fd, D.build_speaker_setup())
    seq = cnt = 0; per = D.PERIOD; nt = time.monotonic(); n = 0
    while not G.stop:
        with G.lock:
            op = G.sound.pop(0) if G.sound else None
            r, g, b, m, t = (*G.rgb, G.motor, G.trig)
        try:
            if op is not None:
                os.write(fd, D.build_0x36(seq, cnt, op)); cnt = (cnt + 1) & 0xFF
            if n % 4 == 0:
                os.write(fd, out_report(seq, r, g, b, m, t))
        except OSError:
            break
        seq = (seq + 1) & 0x0F; n += 1
        nt += per; sl = nt - time.monotonic()
        if sl > 0: time.sleep(sl)
        else: nt = time.monotonic()
    try: os.write(fd, out_report(0, 0, 0, 0, 0, 0))   # LEDs + Trigger frei
    except OSError: pass
    os.close(fd)


class Pad:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.ly = 128; self.r2 = 0; self.btn = 0
    def run(self):
        while not G.stop:
            r, _, _ = select.select([self.fd], [], [], 0.02)
            if not r: continue
            try:
                while True:
                    b = os.read(self.fd, 128)
                    if not b or b[0] != 0x31: continue
                    if len(b) > 5 and b[3] == 0xd4: continue
                    if len(b) > 9: self.ly, self.r2, self.btn = b[3], b[7], b[9]
            except BlockingIOError: pass
        os.close(self.fd)


def alarm_color(a):                                # 0..1: gruen -> gelb -> rot
    a = max(0.0, min(1.0, a))
    if a < 0.5: return (int(255 * (a * 2)), 200, 0)
    return (255, int(200 * (1 - (a - 0.5) * 2)), 0)


def main(scr):
    npins = 4
    if "--pins" in sys.argv: npins = max(1, min(5, int(sys.argv[sys.argv.index("--pins") + 1])))
    path = D.find_dualsense_hidraw()
    if not path:
        scr.addstr(0, 0, "DualSense (BT) nicht gefunden."); scr.getch(); return
    os.system("systemctl --user stop ds5-membrane-sink.service 2>/dev/null")
    os.system("pkill -f ds5_membrane_sink 2>/dev/null"); time.sleep(0.4)

    enc = D.OpusEncoder(); G.silence = enc.encode(bytes(480 * 2 * 2), 480)
    S_click = make_sound(enc, [(1700, 12, 0.5, 1.0, 0.4)])
    S_set   = make_sound(enc, [(900, 18, 0.7, 1.0, 0.2), (240, 70, 0.8, 0.999, 0.25)])
    S_over  = make_sound(enc, [(140, 130, 0.9, 0.9990, 0.5)])
    S_win   = make_sound(enc, [(523, 90, 0.6, 1.0, 0.05), (659, 90, 0.6, 1.0, 0.05),
                               (784, 90, 0.6, 1.0, 0.05), (1047, 200, 0.7, 1.0, 0.05)])
    S_bust  = make_sound(enc, [(880, 160, 0.8, 1.0, 0.1), (590, 160, 0.8, 1.0, 0.1),
                               (880, 160, 0.8, 1.0, 0.1), (590, 240, 0.8, 1.0, 0.1)])

    pad = Pad(path)
    threading.Thread(target=pad.run, daemon=True).start()
    threading.Thread(target=sender, args=(path,), daemon=True).start()
    curses.curs_set(0); scr.nodelay(True); scr.timeout(15)

    def put(y, x, s, a=0):
        my, mx = scr.getmaxyx()
        if 0 <= y < my and x < mx:
            try: scr.addstr(y, x, s[:mx - x - 1], a)
            except curses.error: pass

    rnd = random.Random()
    SET_WIN = 0.07; SET_HOLD = 0.35; OVERSET = 0.14
    while not G.stop:
        targets = [round(rnd.uniform(0.32, 0.82), 3) for _ in range(npins)]
        cur = 0; alarm = 0.0; in_win_since = None; click_armed = True
        last = time.monotonic(); result = None
        while result is None and not G.stop:
            now = time.monotonic(); dt = now - last; last = now
            ch = scr.getch()
            if ch in (ord('q'), 27): G.stop = True; break

            ly, r2 = pad.ly, pad.r2
            pick = max(0.0, min(1.15, (128 - ly) / 110.0))     # Stick hoch = Stift heben
            tension = r2 / 255.0
            tens_ok = tension > 0.15
            t = targets[cur]; dist = abs(pick - t)

            motor = 0; alarm += dt * 0.020                     # Grund-Zeitdruck
            G.trig = 3 + int(alarm * 5)                        # Spannung steigt mit Alarm
            if not tens_ok:
                in_win_since = None; click_armed = True
            else:
                near = max(0.0, 1.0 - dist / 0.45)             # Naehe -> Rumble (heiss/kalt)
                motor = int(40 + near * 150)
                if pick > t + OVERSET:                         # ueberdreht -> Stift faellt
                    play(S_over); G.motor = 230; alarm += 0.16
                    in_win_since = None; click_armed = True
                    time.sleep(0.04)
                elif dist < SET_WIN:                           # an der Scherlinie
                    if click_armed: play(S_click); click_armed = False
                    motor = 200
                    if in_win_since is None: in_win_since = now
                    elif now - in_win_since > SET_HOLD:        # gehalten -> gesetzt!
                        play(S_set); G.rgb = (0, 255, 0)
                        cur += 1; in_win_since = None; click_armed = True
                        if cur >= npins: result = "win"
                        time.sleep(0.05)
                else:
                    in_win_since = None
                    if dist > SET_WIN * 1.6: click_armed = True
            G.motor = motor
            if alarm >= 1.0: result = "bust"
            if result is None: G.rgb = alarm_color(alarm)

            # Anzeige
            scr.erase()
            put(0, 2, "DS5  SAFE CRACKER   (Augen zu, nur fuehlen)", curses.A_BOLD)
            put(2, 2, "R2 = Spannung halten   Stick HOCH = Stift heben   [q] quit")
            bar = "".join("#" if i < cur else ("o" if i == cur else ".") for i in range(npins))
            put(4, 2, "Stifte gesetzt: [%s]  %d/%d" % (bar, cur, npins))
            ph = int(pick * 30); put(6, 2, "Pick  |" + "=" * ph + ">" + " " * (32 - ph) + "|")
            put(7, 2, "Tension R2: %3d%%   %s" % (int(tension * 100), "OK " if tens_ok else "-- locker --"))
            am = int(alarm * 30)
            put(9, 2, "ALARM |" + "!" * am + " " * (30 - am) + "|  %d%%" % int(alarm * 100),
                curses.A_BOLD if alarm > 0.6 else 0)
            scr.refresh()
            time.sleep(0.012)

        if G.stop: break
        # Ende einer Runde
        if result == "win":
            play(S_win); G.rgb = (0, 255, 0); G.motor = 0; G.trig = 0
            msg = "GEKNACKT!  Tresor offen."
        else:
            play(S_bust); G.rgb = (255, 0, 0); G.motor = 150; G.trig = 0
            msg = "ERWISCHT!  Security ist da."
        t_end = time.monotonic()
        while time.monotonic() - t_end < 2.2 and not G.stop:
            scr.erase(); put(1, 2, msg, curses.A_BOLD)
            put(3, 2, "[X / Leertaste] nochmal     [q] quit")
            scr.refresh()
            ch = scr.getch()
            if ch in (ord('q'), 27): G.stop = True
            time.sleep(0.02)
        G.motor = 0
        # warten auf Neustart
        while not G.stop:
            ch = scr.getch()
            if ch in (ord('q'), 27): G.stop = True; break
            if ch == ord(' ') or (pad.btn & 0x20): break       # Leertaste / X
            time.sleep(0.02)
    G.stop = True; time.sleep(0.15)


if __name__ == "__main__":
    try:
        curses.wrapper(main)
    finally:
        G.stop = True
        os.system("systemctl --user start ds5-membrane-sink.service 2>/dev/null")
        print("Audio-Service wieder gestartet.")
