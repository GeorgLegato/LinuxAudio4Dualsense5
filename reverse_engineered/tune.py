#!/usr/bin/env python3
# tune.py — interaktiver DualSense-BT-Speaker-Tuner (analog ~/dualsense_tune.py).
#
# Audio laeuft als ffmpeg|python3-Subprozess; Setup-Bytes werden live
# nachgeschickt; Stream-Bytes (Format, Rate, Vol, PID, Mask) triggern
# einen sauberen Restart.

import curses
import os
import sys
import shutil
import signal
import subprocess
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ps5bt_speaker import (
    build_setup_report, build_release_report,
    PATH_AUTO, PATH_HEADPHONE, PATH_BOTH, PATH_SPEAKER,
    SPEAKER_VOL_MIN, SPEAKER_VOL_MAX,
)

PATH_NAMES = {
    PATH_AUTO:      "0x00  L+R -> Hp,  Speaker MUTE",
    PATH_HEADPHONE: "0x10  L   -> Hp,  Speaker MUTE",
    PATH_BOTH:      "0x20  L   -> Hp,  R -> Speaker",
    PATH_SPEAKER:   "0x30  Hp  MUTE,  R -> Speaker  (PURER SPEAKER)",
}
PATH_CYCLE = [PATH_AUTO, PATH_HEADPHONE, PATH_BOTH, PATH_SPEAKER]

FMT_CYCLE = ["s8", "u8", "mulaw", "alaw", "s16le", "s16be"]
RATE_CYCLE = [4000, 6000, 8000, 10000, 12000, 16000, 20000, 24000]
SIZE_CYCLE = [32, 48, 64, 80, 96, 112, 120]
SOURCE_CYCLE = ["sine", "sine_sweep_100_2000", "pipe"]
# Safe-Whitelist: NUR Werte die im alten mask-sweep ohne BT-Disconnect liefen.
# 0xFF GESPERRT (killt BT). Bit-0-Hypothese: Bit0 = Speaker-Membran-Enable.
# Reihenfolge: Haptik-baseline zuerst, dann Bit0-set-Kandidaten fuer Membran.
INIT_MASKS = [0xFE, 0x01, 0x03, 0x05, 0x07, 0x0F, 0x80]
PIDS = [0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x1F]

state = {
    # Setup-Report  — GOLD-WERTE (sauberer Ton, 12.06.2026)
    "spk_vol":   0x52,
    "preamp":    0,
    "path":      PATH_AUTO,     # 0x00 — bei BT der funktionierende Pfad
    # Audio-Stream
    "pid":       0x12,
    "init_mask": 0xFE,
    "unk":       0,
    "fmt":       "s8",
    "rate":      6000,    # GOLD-WERT: 6000 gibt durchgaengigen Dauerton
    "sample_size": 64,
    "pcm_vol":   2.0,
    "source":    "sine",
    "sine_freq": 440,     # tunebar [g/G] — >1000 Hz = Membran-Beweis (LRA kann das nicht)
    "init_byte5": 0xFF,   # KEIN Gain (verifiziert), SAxense-Default belassen
    "channels":  1,       # DualSense-Speaker ist STEREO -> 2 testen fuer Lautstaerke
    "init_route": 0,      # 0x11-Byte1: Routing-Verdacht Haptik(0) vs Membran(?)
    "full_flags": False,  # valid_flag0=0xFF (USB-Analogie rep[1]=0xFF) — Membran-Test
}

stream_proc = None   # Tuple (ffmpeg, python) Popens, oder None
LOG = None           # Logfile-Handle (Subprozess-stderr + eigene Events)
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tune.log")


def log(msg):
    if LOG is not None:
        LOG.write(msg.rstrip() + "\n")
        LOG.flush()


def find_hidraw():
    for d in os.listdir("/sys/class/hidraw"):
        try:
            with open(f"/sys/class/hidraw/{d}/device/uevent") as f:
                if "HID_ID=0005:0000054C:00000CE6" in f.read():
                    return f"/dev/{d}"
        except OSError:
            pass
    return None


def send_setup(fd):
    pkt = build_setup_report(speaker_vol=state["spk_vol"],
                             preamp=state["preamp"],
                             path=state["path"])
    try:
        os.write(fd, pkt)
    except OSError:
        pass


def fmt_to_bps(fmt):
    return 2 if fmt in ("s16le", "s16be") else 1


def ffmpeg_source_cmd(source, vol_pct, rate, fmt, channels, sine_freq=440):
    """Build ffmpeg cmd that writes raw PCM bytes to stdout, forever."""
    if source == "sine":
        return ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"sine=frequency={sine_freq}:duration=99999",
                "-af", f"volume={vol_pct}",
                "-ac", str(channels), "-ar", str(rate), "-f", fmt, "-"]
    if source == "sine_sweep_100_2000":
        # Echter Glissando: Frequenz wackelt 200<->1200 Hz mit 0.4 Hz Rate.
        # Klar hoerbare Tonhoehenaenderung (vs. beep_factor das nur beept).
        expr = "0.7*sin(2*PI*(700+500*sin(2*PI*0.4*t))*t)"
        return ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi",
                "-i", f"aevalsrc={expr}:s={rate}:d=99999",
                "-af", f"volume={vol_pct}",
                "-ac", str(channels), "-ar", str(rate), "-f", fmt, "-"]
    if source == "pipe":
        # Default-sink monitor via parec | ffmpeg
        return None  # special-cased: handled with shell pipeline
    raise ValueError(source)


PATH_TO_ARG = {PATH_AUTO: "auto", PATH_HEADPHONE: "headphone",
               PATH_BOTH: "both", PATH_SPEAKER: "speaker"}


def start_stream(hidraw):
    global stream_proc
    stop_stream()
    py = sys.executable
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "ps5bt_speaker.py")
    bps = fmt_to_bps(state["fmt"])
    # WICHTIG: der Subprozess macht ALLES (Setup + Audio + Keepalive) ueber
    # EINEN fd. Das TUI schreibt waehrend des Streams NICHT aufs hidraw, sonst
    # verschachteln sich die Writes (Tuet-Pause-Tuet / BRRR). Setup-Params
    # werden hier mit uebergeben; Aenderungen loesen einen Restart aus.
    py_cmd = [py, script, hidraw,
              "--pid", f"0x{state['pid']:02x}",
              "--rate", str(state["rate"]),
              "--channels", str(state["channels"]),
              "--bytes-per-sample", str(bps),
              "--sample-size", str(state["sample_size"]),
              "--init-mask", f"0x{state['init_mask']:02x}",
              "--init-byte5", f"0x{state['init_byte5']:02x}",
              "--unk", str(state["unk"]),
              "--speaker-vol", f"0x{state['spk_vol']:02x}",
              "--preamp", str(state["preamp"]),
              "--path", PATH_TO_ARG[state["path"]],
              "--init-route", f"0x{state['init_route']:02x}",
              "--keepalive-sec", "3"]   # Subprozess schiebt 0x31 sequenziell ein
    if state["fmt"] in ("u8",):
        py_cmd.append("--unsigned")     # Silence-Sample 0x80 statt 0x00
    if state["full_flags"]:
        py_cmd.append("--full-flags")   # valid_flag0=0xFF Membran-Test

    log(f"start_stream: src={state['source']} fmt={state['fmt']} "
        f"rate={state['rate']} ch={state['channels']} vol={state['pcm_vol']} "
        f"path={PATH_TO_ARG[state['path']]} preamp={state['preamp']} "
        f"spk_vol=0x{state['spk_vol']:02x}")
    if state["source"] == "pipe":
        # parec ... | ffmpeg ... | python3 ...
        default_sink = subprocess.check_output(
            ["pactl", "get-default-sink"], text=True).strip()
        mon = default_sink + ".monitor"
        parec = subprocess.Popen(
            ["parec", f"--device={mon}", "--rate=48000",
             "--channels=2", "--format=s16le", "--raw"],
            stdout=subprocess.PIPE, stderr=LOG)
        ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "-",
             "-af", f"volume={state['pcm_vol']}",
             "-ac", str(state["channels"]), "-ar", str(state["rate"]),
             "-f", state["fmt"], "-"],
            stdin=parec.stdout, stdout=subprocess.PIPE, stderr=LOG)
        parec.stdout.close()
        py_proc = subprocess.Popen(py_cmd, stdin=ff.stdout, stderr=LOG)
        ff.stdout.close()
        stream_proc = (parec, ff, py_proc)
    else:
        ff_cmd = ffmpeg_source_cmd(state["source"], state["pcm_vol"],
                                   state["rate"], state["fmt"],
                                   state["channels"], state["sine_freq"])
        ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=LOG)
        py_proc = subprocess.Popen(py_cmd, stdin=ff.stdout, stderr=LOG)
        ff.stdout.close()
        stream_proc = (ff, py_proc)


def stop_stream():
    global stream_proc
    if stream_proc is None:
        return
    log("stop_stream")
    # Producer zuerst terminieren (parec/ffmpeg), dann Consumer (python) —
    # vermeidet broken-pipe-Spam wenn der Consumer vor dem Producer stirbt.
    for p in stream_proc:
        try:
            p.terminate()
        except ProcessLookupError:
            pass
    for p in stream_proc:
        try:
            p.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except ProcessLookupError:
                pass
    stream_proc = None


def stream_running():
    return stream_proc is not None and any(p.poll() is None for p in stream_proc)


def safe_addstr(stdscr, y, x, s, attr=0):
    try:
        h, w = stdscr.getmaxyx()
        if y >= h: return
        s = s[: max(0, w - x - 1)]
        stdscr.addstr(y, x, s, attr) if attr else stdscr.addstr(y, x, s)
    except curses.error:
        pass


def render(stdscr, hidraw):
    stdscr.clear()
    safe_addstr(stdscr, 0, 0,
        "DualSense BT Speaker Tuner — Echtzeit-HID-Tuning", curses.A_BOLD)
    safe_addstr(stdscr, 1, 0, f"  hidraw: {hidraw}   stream: "
                              f"{'RUNNING' if stream_running() else 'stopped'}"
                              f"   log: {LOG_PATH}")

    safe_addstr(stdscr, 3, 2, "[Setup-Report 0x31, live-update]", curses.A_BOLD)
    safe_addstr(stdscr, 4, 4,
        f"spk_vol     = 0x{state['spk_vol']:02X} ({state['spk_vol']:3d})    "
        f"[<- ->]   safe 0x{SPEAKER_VOL_MIN:02X}..0x{SPEAKER_VOL_MAX:02X}")
    safe_addstr(stdscr, 5, 4,
        f"preamp gain = {state['preamp']}             "
        f"[up dn]   0..7  (PS5 typ. 0-2)")
    safe_addstr(stdscr, 6, 4,
        f"path        = {PATH_NAMES[state['path']]}   "
        f"[p / P]")

    safe_addstr(stdscr, 8, 2, "[Audio-Stream, restart on change]", curses.A_BOLD)
    safe_addstr(stdscr, 9, 4,
        f"PCM vol     = {state['pcm_vol']:.2f}        "
        f"[v / V]   ffmpeg pre-amplify")
    safe_addstr(stdscr, 10, 4,
        f"format      = {state['fmt']:6s}        "
        f"[f]       cycle u8/s8/mulaw/alaw/s16le/s16be")
    safe_addstr(stdscr, 11, 4,
        f"rate        = {state['rate']:5d} Hz    "
        f"[r / R]   cycle 4..24 kHz")
    safe_addstr(stdscr, 12, 4,
        f"sample size = {state['sample_size']:3d} byte    "
        f"[z / Z]   cycle 32..120")
    safe_addstr(stdscr, 13, 4,
        f"PID         = 0x{state['pid']:02X}  (FIX)    "
        f"          andere PIDs = Keyboard-Input, gesperrt!")
    safe_addstr(stdscr, 14, 4,
        f"init_mask   = 0x{state['init_mask']:02X}          "
        f"[m / M]   Bit0=Speaker-Verdacht! (0xFF gesperrt=BT-kill)",
        curses.A_BOLD)
    safe_addstr(stdscr, 15, 4,
        f"channels    = {state['channels']}  ({'mono' if state['channels']==1 else 'STEREO'})        "
        f"[c]       Speaker ist stereo -> 2 = lauter?", curses.A_BOLD)
    safe_addstr(stdscr, 16, 4,
        f"init_byte5  = 0x{state['init_byte5']:02X}          "
        f"[b / B]   (kein Gain, lieber 0xFF lassen)")
    safe_addstr(stdscr, 17, 4,
        f"init_route  = 0x{state['init_route']:02X}   "
        f"full_flags={'ON ' if state['full_flags'] else 'off'}  "
        f"[a/A] route  [j] flags=0xFF (USB-Analogie)", curses.A_BOLD)
    safe_addstr(stdscr, 18, 4,
        f"source      = {state['source']:20s}     "
        f"[s]       cycle sine / sweep / pipe")
    safe_addstr(stdscr, 19, 4,
        f"sine freq   = {state['sine_freq']:5d} Hz    "
        f"[g / G]   >1000 Hz = MEMBRAN-Beweis (Haptik kann das nicht)",
        curses.A_BOLD)

    safe_addstr(stdscr, 20, 2, "[SPACE] start/restart stream    [x] stop", curses.A_BOLD)
    safe_addstr(stdscr, 21, 2, "[t] re-send setup-report (manual)   [n] release/silence")
    safe_addstr(stdscr, 22, 2, "[q] quit (release + LED gruen)")

    safe_addstr(stdscr, 24, 2, "Lautstaerke: [c] stereo testen, [v/V] PCM-vol. Sinus sauber.")
    stdscr.refresh()


def cycle_next(lst, cur):
    try:
        return lst[(lst.index(cur) + 1) % len(lst)]
    except ValueError:
        return lst[0]


def cycle_prev(lst, cur):
    try:
        return lst[(lst.index(cur) - 1) % len(lst)]
    except ValueError:
        return lst[0]


def main(stdscr):
    global LOG
    LOG = open(LOG_PATH, "w", buffering=1)   # line-buffered, frisch pro Session
    log("=== tune.py session start ===")

    curses.curs_set(0)
    # nodelay + timeout: getch() kehrt nach 300ms mit -1 zurueck, damit die
    # Loop auch ohne Tastendruck weiterlaeuft.
    stdscr.nodelay(True)
    stdscr.timeout(300)
    stdscr.keypad(True)

    hidraw = find_hidraw()
    if not hidraw:
        stdscr.nodelay(False)
        stdscr.addstr(0, 0, "DualSense BT hidraw nicht gefunden.")
        stdscr.getch()
        return
    fd = os.open(hidraw, os.O_WRONLY)
    send_setup(fd)

    needs_restart = False

    try:
        while True:
            render(stdscr, hidraw)
            ch = stdscr.getch()

            # Kein TUI-Keepalive mehr: der Subprozess macht das selbst (ein fd).
            # Timeout-Tick ohne Taste -> nur neu rendern (zeigt stream-Status).
            if ch == -1:
                continue

            # ===== Quit =====
            if ch in (ord('q'), 27):
                break

            # ===== Setup-Report bytes =====
            # Waehrend Stream: Restart (Subprozess haelt die Params, nur EIN fd
            # darf schreiben). Ohne Stream: direkter Write fuer LED-Feedback.
            elif ch == curses.KEY_RIGHT:
                state["spk_vol"] = min(SPEAKER_VOL_MAX, state["spk_vol"] + 1)
                needs_restart = True if stream_running() else (send_setup(fd) or False)
            elif ch == curses.KEY_LEFT:
                state["spk_vol"] = max(0, state["spk_vol"] - 1)
                needs_restart = True if stream_running() else (send_setup(fd) or False)
            elif ch == curses.KEY_UP:
                state["preamp"] = min(7, state["preamp"] + 1)
                needs_restart = True if stream_running() else (send_setup(fd) or False)
            elif ch == curses.KEY_DOWN:
                state["preamp"] = max(0, state["preamp"] - 1)
                needs_restart = True if stream_running() else (send_setup(fd) or False)
            elif ch == ord('p'):
                state["path"] = cycle_next(PATH_CYCLE, state["path"])
                needs_restart = True if stream_running() else (send_setup(fd) or False)
            elif ch == ord('P'):
                state["path"] = cycle_prev(PATH_CYCLE, state["path"])
                needs_restart = True if stream_running() else (send_setup(fd) or False)
            elif ch == ord('t'):
                if not stream_running():
                    send_setup(fd)   # manual re-send (nur ohne Stream)

            # ===== Stream-Parameter (restart) =====
            elif ch == ord('v'):
                state["pcm_vol"] = max(0.0, round(state["pcm_vol"] - 0.05, 2))
                needs_restart = True
            elif ch == ord('V'):
                state["pcm_vol"] = min(2.0, round(state["pcm_vol"] + 0.05, 2))
                needs_restart = True
            elif ch == ord('f'):
                state["fmt"] = cycle_next(FMT_CYCLE, state["fmt"])
                needs_restart = True
            elif ch == ord('F'):
                state["fmt"] = cycle_prev(FMT_CYCLE, state["fmt"])
                needs_restart = True
            elif ch == ord('r'):
                state["rate"] = cycle_next(RATE_CYCLE, state["rate"])
                needs_restart = True
            elif ch == ord('R'):
                state["rate"] = cycle_prev(RATE_CYCLE, state["rate"])
                needs_restart = True
            elif ch == ord('z'):
                state["sample_size"] = cycle_next(SIZE_CYCLE, state["sample_size"])
                needs_restart = True
            elif ch == ord('Z'):
                state["sample_size"] = cycle_prev(SIZE_CYCLE, state["sample_size"])
                needs_restart = True
            # init_mask: Safe-Whitelist (0xFF gesperrt, killt BT). Membran-Jagd:
            # Bit-0-Hypothese (Bit0 = Speaker-Membran-Enable).
            elif ch == ord('m'):
                state["init_mask"] = cycle_next(INIT_MASKS, state["init_mask"])
                needs_restart = True
            elif ch == ord('M'):
                state["init_mask"] = cycle_prev(INIT_MASKS, state["init_mask"])
                needs_restart = True
            elif ch == ord('b'):
                state["init_byte5"] = max(0x00, state["init_byte5"] - 0x10)
                needs_restart = True
            elif ch == ord('B'):
                state["init_byte5"] = min(0xFF, state["init_byte5"] + 0x10)
                needs_restart = True
            elif ch == ord('c'):
                state["channels"] = 2 if state["channels"] == 1 else 1
                needs_restart = True
            elif ch == ord('u'):
                state["unk"] ^= 1
                needs_restart = True
            elif ch == ord('a'):
                state["init_route"] = max(0x00, state["init_route"] - 1)
                needs_restart = True
            elif ch == ord('A'):
                state["init_route"] = min(0xFF, state["init_route"] + 1)
                needs_restart = True
            elif ch == ord('j'):
                state["full_flags"] = not state["full_flags"]
                needs_restart = True
            elif ch == ord('s'):
                state["source"] = cycle_next(SOURCE_CYCLE, state["source"])
                needs_restart = True
            elif ch == ord('g'):
                state["sine_freq"] = max(50, state["sine_freq"] - 100)
                needs_restart = True
            elif ch == ord('G'):
                state["sine_freq"] = min(4000, state["sine_freq"] + 100)
                needs_restart = True

            # ===== Stream-Aktionen =====
            # start_stream macht Setup+Audio+Keepalive im Subprozess. Das TUI
            # schreibt NICHT zusaetzlich (kein paralleler fd-Write).
            elif ch == ord(' '):
                start_stream(hidraw)
                needs_restart = False
            elif ch == ord('x'):
                stop_stream()
                # nach Stream-Stop darf das TUI wieder schreiben
                try:
                    os.write(fd, build_release_report())
                except OSError:
                    pass
            elif ch == ord('n'):
                stop_stream()
                try:
                    os.write(fd, build_release_report())
                except OSError:
                    pass

            # Apply pending restart if stream is currently running
            if needs_restart and stream_running():
                start_stream(hidraw)
                needs_restart = False
    finally:
        log("=== session end, release + close ===")
        stop_stream()
        try:
            os.write(fd, build_release_report())
        except OSError:
            pass
        os.close(fd)
        if LOG is not None:
            LOG.close()


if __name__ == "__main__":
    if shutil.which("ffmpeg") is None:
        print("ffmpeg fehlt", file=sys.stderr); sys.exit(1)
    if shutil.which("parec") is None:
        print("parec fehlt (Pulseaudio-Tools)", file=sys.stderr); sys.exit(1)
    curses.wrapper(main)
