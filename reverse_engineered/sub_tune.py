#!/usr/bin/python3
# sub_tune.py — DualSense "subwoofer" tuner: full range -> membrane speaker,
#               low-passed bass -> haptic actuators (body-sound subwoofer).
#
# The membrane speaker has no real bass. But the DualSense's two voice-coil
# haptic actuators ARE essentially body-sound subwoofers — a 50 Hz sine on the
# haptic route is felt as a strong, deep thump. This tool taps the system audio,
# sends the full range to the membrane (Opus, report 0x13, exactly as the
# production sink) AND a low-passed copy (~200 Hz, tunable) to the haptic route
# (report 0x12, 64 int8 samples @ 6 kHz per packet). On music with a kick/bass
# drum this gives the controller a real low end.
#
# This is the EXPERIMENTATION tool: tune the cutoff / gain / amp cap by ear,
# then fold the chosen values into the native sink. It must be the sole 0x36
# sender, so it stops the systemd audio service while it runs.
#
# Signal path per 512-frame block (48 kHz stereo, = one 0x36 packet, 10.667 ms):
#   membrane: resample 512->480 -> Opus CBR 160k -> 0x13   (full range)
#   haptic:   mono -> biquad low-pass(fc) -> DC-block -> decimate /8 -> 64xint8 -> 0x12
#   512 / 8 = 64  == haptic SAMPLE_SIZE exactly (one block -> one haptic frame).
#
# Keys: [f/F] cutoff -/+   [g/G] haptic gain -/+   [a/A] amp cap -/+
#       [h] haptic on/off  [m] membrane on/off     [SPACE] pause   [q] quit
#
#   ./sub_tune.py [--source MONITOR] [--cutoff HZ] [--gain X] [--amp N]

import os, sys, math, time, queue, threading, subprocess, curses

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ps5bt_membrane as ds5      # reuse the proven membrane path verbatim

BLOCK_BYTES  = ds5.PCM_BYTES_PER_FRAME    # 2048 = 512 frames * 2ch * 2byte
HAPTIC_N     = ds5.SAMPLE_SIZE            # 64 samples per haptic frame
DECIM        = ds5.INPUT_BLOCK_FRAMES // HAPTIC_N   # 512/64 = 8 (48k -> 6k)
PERIOD       = ds5.PERIOD                 # 10.667 ms


class Biquad:
    """RBJ low-pass biquad, Q=0.707 (Butterworth), direct-form I, stateful."""
    def __init__(self, fc, fs=48000.0):
        self.fs = fs
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0
        self.set_cutoff(fc)

    def set_cutoff(self, fc):
        fc = max(20.0, min(fc, self.fs / 2 - 100))
        w0 = 2 * math.pi * fc / self.fs
        cw, sw = math.cos(w0), math.sin(w0)
        alpha = sw / (2 * 0.7071)
        b0 = (1 - cw) / 2; b1 = 1 - cw; b2 = (1 - cw) / 2
        a0 = 1 + alpha;    a1 = -2 * cw; a2 = 1 - alpha
        self.b0, self.b1, self.b2 = b0 / a0, b1 / a0, b2 / a0
        self.a1, self.a2 = a1 / a0, a2 / a0
        self.fc = fc

    def step(self, x):
        y = (self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
             - self.a1 * self.y1 - self.a2 * self.y2)
        self.x2, self.x1 = self.x1, x
        self.y2, self.y1 = self.y1, y
        return y


class Sub:
    def __init__(self, args):
        self.path = ds5.find_dualsense_hidraw()
        self.q = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.lp = Biquad(args.cutoff)
        self.cutoff = args.cutoff
        self.gain = args.gain          # multiplier on normalized mono before amp cap
        self.amp = args.amp            # int8 amplitude cap (USB membrane used 64)
        self.haptic_on = True
        self.membrane_on = True
        self.paused = False
        self.source = args.source
        self.dc_x1 = self.dc_y1 = 0.0  # one-pole DC blocker state
        self.enc = ds5.OpusEncoder(rate=48000, channels=2)
        self.blocks = 0
        self.peak = 0
        self._silence = bytes(BLOCK_BYTES)

    # --- system-audio capture (default sink monitor unless overridden) --------
    def default_monitor(self):
        if self.source:
            return self.source
        try:
            sink = subprocess.check_output(["pactl", "get-default-sink"],
                                           text=True).strip()
            return sink + ".monitor"
        except Exception:
            return "@DEFAULT_MONITOR@"

    def reader(self):
        mon = self.default_monitor()
        p = subprocess.Popen(
            ["parec", "-d", mon, "--format=s16le", "--rate=48000",
             "--channels=2", "--latency-msec=20"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while not self.stop.is_set():
                buf = b""
                while len(buf) < BLOCK_BYTES and not self.stop.is_set():
                    chunk = p.stdout.read(BLOCK_BYTES - len(buf))
                    if not chunk:
                        return
                    buf += chunk
                try:
                    self.q.put_nowait(buf)
                except queue.Full:
                    try: self.q.get_nowait()      # drop oldest, stay low-latency
                    except queue.Empty: pass
                    try: self.q.put_nowait(buf)
                    except queue.Full: pass
        finally:
            p.terminate()

    # --- haptic bass: mono -> LP -> DC-block -> decimate /8 -> 64 int8 --------
    def make_haptic(self, pcm):
        import array
        a = array.array('h'); a.frombytes(pcm)
        out = bytearray(HAPTIC_N)
        peak = 0
        amp = self.amp
        g = self.gain
        for i in range(HAPTIC_N):
            base = (i * DECIM) << 1
            last = 0.0
            # filter all DECIM samples of this group; keep the last (post-LP)
            for j in range(DECIM):
                b = base + (j << 1)
                mono = (a[b] + a[b + 1]) * 0.5
                last = self.lp.step(mono)
            # one-pole DC blocker (~ removes sub-20Hz hold/offset on the coil)
            dc = last - self.dc_x1 + 0.995 * self.dc_y1
            self.dc_x1, self.dc_y1 = last, dc
            v = int(dc / 32768.0 * g * amp)
            if v > amp: v = amp
            elif v < -amp: v = -amp
            if v < 0: v += 256        # int8 -> unsigned byte
            out[i] = v
            av = v if v < 128 else 256 - v
            if av > peak: peak = av
        self.peak = peak
        return bytes(out)

    def sender(self):
        try:
            fd = os.open(self.path, os.O_WRONLY)
        except OSError:
            return
        os.write(fd, ds5.build_speaker_setup())
        seq = counter = 0
        nt = time.monotonic()
        last = self._silence
        while not self.stop.is_set():
            try:
                pcm = self.q.get_nowait(); last = pcm
            except queue.Empty:
                pcm = last
            if self.paused:
                pcm = self._silence
            # membrane (full range)
            if self.membrane_on:
                rs = ds5.resample_512_to_480(pcm)
                opus = self.enc.encode(rs, ds5.OPUS_FRAME_SAMPLES)
            else:
                rs = ds5.resample_512_to_480(self._silence)
                opus = self.enc.encode(rs, ds5.OPUS_FRAME_SAMPLES)
            # haptic (bass)
            hap = self.make_haptic(pcm) if self.haptic_on else bytes(HAPTIC_N)
            try:
                os.write(fd, ds5.build_0x36(seq, counter, opus, haptik=hap))
            except OSError:
                break
            seq = (seq + 1) & 0x0F
            counter = (counter + 1) & 0xFF
            self.blocks += 1
            nt += PERIOD
            sl = nt - time.monotonic()
            if sl > 0:
                time.sleep(sl)
            else:
                nt = time.monotonic()
        os.close(fd)


def tui(scr, sub):
    curses.curs_set(0); scr.nodelay(True); scr.timeout(250)
    last_t = time.monotonic(); last_b = 0; bps = 0
    while True:
        now = time.monotonic()
        if now - last_t >= 1.0:
            bps = sub.blocks - last_b; last_b = sub.blocks; last_t = now
        scr.erase()
        scr.addstr(0, 0, "DualSense Subwoofer-Tuner  (Bass -> Haptik, Vollband -> Membran)",
                   curses.A_BOLD)
        scr.addstr(1, 0, f"hidraw {sub.path}   {bps:3d} pkt/s   "
                          f"{'PAUSE' if sub.paused else 'play '}")
        scr.addstr(3, 2, f"low-pass cutoff = {sub.lp.fc:6.1f} Hz   [f/F]  -/+ 10",
                   curses.A_BOLD)
        scr.addstr(4, 2, f"haptic gain     = {sub.gain:6.2f}      [g/G]  -/+ 0.1")
        scr.addstr(5, 2, f"amp cap (int8)  = {sub.amp:6d}      [a/A]  -/+ 4  (max 127)")
        bar = "#" * min(40, int(sub.peak / 127 * 40))
        scr.addstr(6, 2, f"haptic peak     = {sub.peak:6d}  |{bar:<40}|")
        scr.addstr(8, 2, f"haptic  : {'ON ' if sub.haptic_on else 'off'}  [h]")
        scr.addstr(9, 2, f"membrane: {'ON ' if sub.membrane_on else 'off'}  [m]")
        scr.addstr(11, 2, "[SPACE] pause   [q] quit", curses.A_DIM)
        scr.addstr(13, 2, "Tipp: Musik mit Bassdrum abspielen; cutoff 120-200 Hz,",
                   curses.A_DIM)
        scr.addstr(14, 2, "gain hoch bis es bei Kicks kraeftig wummert, ohne Dauer-Brumm.",
                   curses.A_DIM)
        scr.refresh()
        ch = scr.getch()
        if ch == -1:
            continue
        if ch in (ord('q'), 27):
            break
        elif ch == ord('f'): sub.lp.set_cutoff(sub.lp.fc - 10)
        elif ch == ord('F'): sub.lp.set_cutoff(sub.lp.fc + 10)
        elif ch == ord('g'): sub.gain = max(0.0, round(sub.gain - 0.1, 2))
        elif ch == ord('G'): sub.gain = round(sub.gain + 0.1, 2)
        elif ch == ord('a'): sub.amp = max(1, sub.amp - 4)
        elif ch == ord('A'): sub.amp = min(127, sub.amp + 4)
        elif ch == ord('h'): sub.haptic_on = not sub.haptic_on
        elif ch == ord('m'): sub.membrane_on = not sub.membrane_on
        elif ch == ord(' '): sub.paused = not sub.paused
    sub.stop.set()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="DualSense subwoofer (haptic bass) tuner")
    ap.add_argument("--source", default=None,
                    help="PulseAudio source to capture (default: default sink .monitor)")
    ap.add_argument("--cutoff", type=float, default=180.0, help="low-pass cutoff Hz")
    ap.add_argument("--gain", type=float, default=2.0, help="haptic gain multiplier")
    ap.add_argument("--amp", type=int, default=64, help="int8 amplitude cap (<=127)")
    args = ap.parse_args()

    # be the sole 0x36 sender — stop the production service while tuning
    os.system("systemctl --user stop ds5-membrane-sink.service 2>/dev/null")
    os.system("pkill -f ds5_membrane_sink 2>/dev/null")
    time.sleep(0.4)

    sub = Sub(args)
    if not sub.path:
        print("DualSense (BT) hidraw nicht gefunden.", file=sys.stderr)
        return 1
    threading.Thread(target=sub.reader, daemon=True).start()
    threading.Thread(target=sub.sender, daemon=True).start()
    try:
        curses.wrapper(tui, sub)
    finally:
        sub.stop.set(); time.sleep(0.2)
    print("Service wieder starten:  systemctl --user start ds5-membrane-sink.service",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
