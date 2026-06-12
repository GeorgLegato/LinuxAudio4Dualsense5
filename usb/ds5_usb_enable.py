#!/usr/bin/env python3
"""
DualSense-Speaker-Enable Hack via HID Output Report 0x02.

Linux's snd_usb_audio liefert die 4-Kanal-Audio-Daten, aber Sonys Default
schaltet den eingebauten Speaker stumm. Dieses Skript setzt die fehlenden
Audio-Routing-Bytes 5-8 im USB-Output-Report 0x02, sodass der Speaker
aktiviert wird.

Nutzung:
    python3 dualsense_speaker.py           # speaker an, volume 100%, keep-alive
    python3 dualsense_speaker.py off       # speaker aus
    python3 dualsense_speaker.py 50        # volume 50% (0-100)

Strg+C beendet — Default-Sony-State wird wiederhergestellt.

Audio-Routing: Mit Speaker an, gehen Channel 0/1 (FL/FR) des
USB-Audio-Streams auf den internen Speaker. Channel 2/3 (RL/RR) bleiben
Haptic-Trigger.

Im PipeWire: Default-Sink auf surround-40 stellen, ODER einen Remap-Sink
fuer FL/FR statt RL/RR erzeugen.
"""

import sys
import time
import signal
from pydualsense import pydualsense
from pydualsense.enums import ConnectionType


def patch_speaker_bytes(ds, enable: bool, volume_pct: int):
    """Monkey-patch prepareReport um Speaker-Routing-Bytes zu setzen.

    WICHTIG: PS5-Firmware nutzt nur Speaker-Volume-Range 0x3D-0x64 (61-100).
    Werte darueber uebersteuern den eingebauten Speaker-Verstaerker massiv
    (Quelle: PCGamingWiki DualSense Controller talk page, Reverse-Engineering).
    Wir mappen daher 0..100% → 0x3D..0x64 statt 0..0x7F.
    """
    if volume_pct <= 0:
        vol = 0
    else:
        v_norm = max(0, min(100, volume_pct)) / 100.0
        vol = int(0x3D + v_norm * (0x64 - 0x3D))   # 61..100 statt 0..127
    orig = ds.prepareReport

    def patched():
        rep = orig()
        if rep and rep[0] == 0x02:  # USB Output Report
            # flags1 [1]: 0x10 audio vol mod + 0x20 internal speaker toggle
            #             schon von pydualsense gesetzt (0xFF)
            # flags2 [2]: 0x02 audio/mic mute toggle nicht setzen (sonst mute)
            rep[2] = (rep[2] | 0x04) & ~0x02  # touchpad LED-Strips an, mute aus

            # Audio bytes 5-9 (aus Linux 6.18 hid-playstation Patch):
            #   [5] = headphone volume    (0-127)
            #   [6] = speaker volume      (0-127)
            #   [7] = microphone volume   (0-127)
            #   [8] = audio_control
            #         bits 4-5 = OUTPUT_PATH_SEL:
            #            0b00 (0x00) = L-R X  (L+R → Kopfhörer, Speaker mute) Sony-Default
            #            0b01 (0x10) = L-L X  (L → Kopfhörer, Speaker mute)
            #            0b10 (0x20) = L-L R  (L → Kopfhörer, R → Speaker)
            #            0b11 (0x30) = X-X R  (Kopfhörer mute, R → Speaker) PURER SPEAKER
            #   [9] = audio_control2
            #         bits 0-2 = SP_PREAMP_GAIN (0..7, je höher desto mehr Gain)
            if enable:
                rep[5] = 0x7F   # headphone vol
                rep[6] = vol    # speaker volume (auf 0x3D-0x64 gemappt, PS5-Range)
                rep[7] = 0x40   # mic vol mittig
                rep[8] = 0x30   # PATH_SEL = 3 = X-X R = reiner Speaker
                rep[9] = 0x00   # SP_PREAMP_GAIN = 0 (kein Vorverstaerker, PS5-default-Hypothese)
            else:
                rep[5] = 0x00
                rep[6] = 0x00
                rep[7] = 0x00
                rep[8] = 0x00
                rep[9] = 0x00

        return bytearray(rep) if isinstance(rep, (list, tuple)) else rep

    ds.prepareReport = patched


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "on"
    if arg.lower() == "off":
        enable, vol = False, 0
    elif arg.lower() in ("on", "an"):
        enable, vol = True, 100
    else:
        try:
            enable, vol = True, int(arg)
        except ValueError:
            print(f"Usage: {sys.argv[0]} [on|off|<0..100>]")
            return 1

    print(f"DualSense-Speaker: enable={enable}  volume={vol}%")
    ds = pydualsense()
    ds.init()

    if ds.conType != ConnectionType.USB:
        print(f"FEHLER: Controller ist '{ds.conType}', nicht USB. "
              "Bitte per USB anschliessen.")
        ds.close()
        return 1

    patch_speaker_bytes(ds, enable, vol)
    print("HID-Report-Patch aktiv. Strg+C zum Beenden.")
    print("Spiele jetzt Audio ab — sollte aus dem eingebauten Speaker kommen.")

    def cleanup(*_):
        print("\nWiederherstelle Default-State (Speaker aus)...")
        patch_speaker_bytes(ds, False, 0)
        time.sleep(0.3)
        ds.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main() or 0)
