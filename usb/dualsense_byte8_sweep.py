#!/usr/bin/env python3
"""
Iteriere über Byte [8] Werte mit 4s lautem Test-Ton pro Wert.
User soll genau hören wann was wo rauskommt.
"""
import sys
import os
import time
import wave
import struct
import math
import tempfile
import subprocess
from pydualsense import pydualsense
from pydualsense.enums import ConnectionType


SWEEP_VALUES = [
    0x00, 0x01, 0x02, 0x04, 0x05, 0x10, 0x14, 0x20, 0x21, 0x24,
    0x30, 0x40, 0x44, 0x50, 0x60, 0x64, 0x70, 0x80, 0xA0, 0xC0, 0xE0, 0xFF,
]


def make_patcher(byte8_value, orig_prepare):
    def patched():
        rep = orig_prepare()
        if rep and rep[0] == 0x02:
            rep[2] = (rep[2] | 0x04) & ~0x02
            rep[5] = 0x7F  # headphone vol max
            rep[6] = 0x7F  # speaker vol max
            rep[7] = 0x40  # mic vol mid
            rep[8] = byte8_value
        return rep
    return patched


def make_sine_wav(path, freq=600, duration=4.0, rate=48000):
    """Generate stereo sine wave WAV file."""
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        amp = 25000  # < 32767 to avoid clipping
        for i in range(int(rate * duration)):
            v = int(amp * math.sin(2 * math.pi * freq * i / rate))
            w.writeframesraw(struct.pack('<hh', v, v))


def main():
    ds = pydualsense()
    ds.init()
    if ds.conType != ConnectionType.USB:
        print("Need USB connection", flush=True)
        ds.close()
        return 1

    # Sine-Wav vorab erzeugen
    wav_path = "/tmp/sine600.wav"
    make_sine_wav(wav_path, freq=600, duration=3.5)

    print("Sweep startet — 4s pro Wert, lauter Sinus-Ton.", flush=True)
    print("Hör nach: Speaker-Grille (Unterseite) ODER Jack ODER nichts.\n", flush=True)
    time.sleep(1)

    orig = ds.prepareReport
    for val in SWEEP_VALUES:
        ds.prepareReport = make_patcher(val, orig)
        print(f"  Byte[8] = 0x{val:02X} ...", flush=True)
        # 3-4s tone via PulseAudio (paplay nutzt default-sink)
        subprocess.run(["paplay", wav_path], capture_output=True, timeout=5)
        time.sleep(0.5)

    # Reset to neutral
    ds.prepareReport = orig
    ds.close()
    print("\nFertig.", flush=True)


if __name__ == "__main__":
    sys.exit(main() or 0)
