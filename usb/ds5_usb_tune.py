#!/usr/bin/env python3
"""
DualSense-Speaker-Tuner — interaktive Echtzeit-Justierung der HID-Bytes.

Bedienung (Tasten):
   ←/→   speaker volume (byte 6, HW)      step 1
   ↑/↓   preamp gain    (byte 9, bits 0-2) 0..7
   p/P   path-sel mode  (byte 8, bits 4-5) 0..3
   v/V   pa-software-volume DS_MonoR       -10/+10 %
   l/L   pa-software-volume DS_Speaker      -10/+10 %
   m/M   mic vol        (byte 7)            -8/+8
   h/H   headphone vol  (byte 5)            -8/+8
   t     test-ton paplay sine 1kHz 1s
   r     reset auf Sony-default Werte
   q     quit (Speaker bleibt aktiviert mit aktuellen Werten)
"""
import curses
import subprocess
import time
import threading
import sys
from pydualsense import pydualsense
from pydualsense.enums import ConnectionType

PATH_NAMES = {
    0x00: "L-R X  (L+R to Hp,  Speaker MUTE)",
    0x10: "L-L X  (L   to Hp,  Speaker MUTE)",
    0x20: "L-L R  (L   to Hp,  R to Speaker)",
    0x30: "X-X R  (Hp MUTE,    R to Speaker)",
}


state = {
    "spk_vol":   0x64,    # PS5-typical max
    "preamp":    0x00,    # gain bits 0-2 in byte 9
    "path":      0x30,    # bits 4-5 in byte 8 (0x00/0x10/0x20/0x30)
    "mic_vol":   0x40,
    "hp_vol":    0x7F,
}


def make_patched(orig):
    def patched():
        rep = orig()
        if rep and rep[0] == 0x02:
            rep[2] = (rep[2] | 0x04) & ~0x02
            rep[5] = state["hp_vol"]
            rep[6] = state["spk_vol"]
            rep[7] = state["mic_vol"]
            rep[8] = state["path"]
            rep[9] = state["preamp"]
        return rep
    return patched


def pa_vol_get(sink):
    try:
        out = subprocess.check_output(["pactl", "get-sink-volume", sink], text=True)
        # Volume: front-left: 65536 / 100% / 0,00 dB ...
        pct = out.split("%")[0].split("/")[-1].strip()
        return int(pct.replace(" ", ""))
    except Exception:
        return -1


def pa_vol_set(sink, pct):
    pct = max(0, min(200, pct))
    subprocess.run(["pactl", "set-sink-volume", sink, f"{pct}%"], capture_output=True)


def test_tone():
    subprocess.Popen(
        ["paplay", "--volume", "65536", "/tmp/sine1k.wav"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def safe_addstr(stdscr, y, x, s, attr=0):
    """Add string only if it fits in the terminal."""
    try:
        h, w = stdscr.getmaxyx()
        if y >= h:
            return
        s = s[: max(0, w - x - 1)]
        if attr:
            stdscr.addstr(y, x, s, attr)
        else:
            stdscr.addstr(y, x, s)
    except curses.error:
        pass


def render(stdscr, ds_status):
    stdscr.clear()
    safe_addstr(stdscr, 0, 0, "DualSense-Speaker-Tuner -- Echtzeit-HID-Tuning", curses.A_BOLD)
    safe_addstr(stdscr, 1, 0, f"  Connection: {ds_status}")
    safe_addstr(stdscr, 3, 2, "HID Output Report 0x02:")
    safe_addstr(stdscr, 4, 4, f"byte[5] hp_vol      = 0x{state['hp_vol']:02X} ({state['hp_vol']:3d})  [h/H +-8]")
    safe_addstr(stdscr, 5, 4, f"byte[6] spk_vol     = 0x{state['spk_vol']:02X} ({state['spk_vol']:3d})  [<-/-> +-1]  PS5 range 0x3D-0x64")
    safe_addstr(stdscr, 6, 4, f"byte[7] mic_vol     = 0x{state['mic_vol']:02X} ({state['mic_vol']:3d})  [m/M +-8]")
    safe_addstr(stdscr, 7, 4, f"byte[8] path        = 0x{state['path']:02X}        [p/P cycle]")
    safe_addstr(stdscr, 8, 7, f"  {PATH_NAMES.get(state['path'], '???')}")
    safe_addstr(stdscr, 9, 4, f"byte[9] preamp_gain = 0x{state['preamp']:02X} ({state['preamp']})    [up/down +-1]  PS5 uses 0-2")

    safe_addstr(stdscr, 11, 2, "PipeWire Software-Volume:")
    safe_addstr(stdscr, 12, 4, f"DS_MonoR   = {pa_vol_get('DS_MonoR'):3d}%   [v/V +-10]")
    safe_addstr(stdscr, 13, 4, f"DS_Speaker = {pa_vol_get('DS_Speaker'):3d}%   [l/L +-10]")

    safe_addstr(stdscr, 15, 2, "[t] test-tone 1kHz   [r] reset Sony-default   [q] quit")
    safe_addstr(stdscr, 17, 0, "Tipp: Audio im Hintergrund laufen lassen, dann live tunen.")
    stdscr.refresh()


def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.keypad(True)

    # 1kHz Test-Ton vorab erzeugen
    if not subprocess.run(["test", "-f", "/tmp/sine1k.wav"]).returncode == 0:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
             "-ac", "2", "/tmp/sine1k.wav"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    ds = pydualsense()
    ds.init()
    if ds.conType != ConnectionType.USB:
        stdscr.addstr(0, 0, f"USB nötig, conType={ds.conType}")
        stdscr.getch()
        return
    orig = ds.prepareReport
    ds.prepareReport = make_patched(orig)
    status = f"USB ✓ ({ds.conType})"

    try:
        while True:
            render(stdscr, status)
            ch = stdscr.getch()

            if ch in (ord('q'), 27):  # q or ESC
                break
            elif ch == curses.KEY_RIGHT:
                state["spk_vol"] = min(127, state["spk_vol"] + 1)
            elif ch == curses.KEY_LEFT:
                state["spk_vol"] = max(0, state["spk_vol"] - 1)
            elif ch == curses.KEY_UP:
                state["preamp"] = min(7, state["preamp"] + 1)
            elif ch == curses.KEY_DOWN:
                state["preamp"] = max(0, state["preamp"] - 1)
            elif ch == ord('p'):
                modes = [0x00, 0x10, 0x20, 0x30]
                i = modes.index(state["path"]) if state["path"] in modes else 0
                state["path"] = modes[(i + 1) % len(modes)]
            elif ch == ord('P'):
                modes = [0x00, 0x10, 0x20, 0x30]
                i = modes.index(state["path"]) if state["path"] in modes else 0
                state["path"] = modes[(i - 1) % len(modes)]
            elif ch == ord('m'):
                state["mic_vol"] = max(0, state["mic_vol"] - 8)
            elif ch == ord('M'):
                state["mic_vol"] = min(127, state["mic_vol"] + 8)
            elif ch == ord('h'):
                state["hp_vol"] = max(0, state["hp_vol"] - 8)
            elif ch == ord('H'):
                state["hp_vol"] = min(127, state["hp_vol"] + 8)
            elif ch == ord('v'):
                pa_vol_set("DS_MonoR", pa_vol_get("DS_MonoR") - 10)
            elif ch == ord('V'):
                pa_vol_set("DS_MonoR", pa_vol_get("DS_MonoR") + 10)
            elif ch == ord('l'):
                pa_vol_set("DS_Speaker", pa_vol_get("DS_Speaker") - 10)
            elif ch == ord('L'):
                pa_vol_set("DS_Speaker", pa_vol_get("DS_Speaker") + 10)
            elif ch == ord('t'):
                threading.Thread(target=test_tone, daemon=True).start()
            elif ch == ord('r'):
                state["spk_vol"] = 0x64
                state["preamp"] = 0x00
                state["path"] = 0x30
                state["mic_vol"] = 0x40
                state["hp_vol"] = 0x7F
    finally:
        # Beim Quit Speaker an lassen mit letzten Werten — KEIN cleanup-to-off
        ds.close()


if __name__ == "__main__":
    curses.wrapper(main)
