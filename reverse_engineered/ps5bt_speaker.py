#!/usr/bin/env python3
# ps5bt_speaker.py — DualSense Speaker ueber Bluetooth, Lucky-Shot.
#
# Beruht auf:
#   - SAxense (egormanga) Report-0x32-Container + Sub-Packet-Format (PID 0x11 init, 0x12 haptik)
#   - hid-playstation Kernel-Treiber Report-0x31-Layout + audio_control/valid_flag-Konstanten
#   - CRC-32 mit Sony-Seed 0xA2 == zlib.crc32(bytes([0xA2]) + data)
#
# Was bekannt ist:
#   - PID 0x12 = Haptik-LRA-PCM (3 kHz u8 stereo)
#   - PID fuer Speaker-Membran: UNBEKANNT, muss erraten werden
#   - Vermutung: 0x10 / 0x13 / 0x14 / 0x15 / 0x16 / 0x17 / 0x18
#
# Erst Setup-Report 0x31 senden (Speaker forcieren, Volume hoch),
# dann PCM-Stream in 0x32-Reports mit gewaehltem Sub-Packet-PID.

import os
import sys
import time
import zlib
import struct
import argparse
import signal

# --- Report 0x31 (78 byte total, BT-Standard-Output) -------------------------
# Byte  0     : report_id = 0x31
# Byte  1     : seq_tag
# Byte  2     : tag
# Byte  3..49 : common (47 byte)
#               +0 valid_flag0
#               +1 valid_flag1
#               +2 motor_right
#               +3 motor_left
#               +4 headphone_volume   (0..0x7f)
#               +5 speaker_volume     (0..0xff)
#               +6 mic_volume         (0..0x40)
#               +7 audio_control       bits 4-5 = OUTPUT_PATH_SEL
#               +8 mute_button_led
#               +9 power_save_control
#               +10..36 reserved2[27]
#               +37 audio_control2     bits 0-2 = SP_PREAMP_GAIN
#               +38 valid_flag2
#               +39..40 reserved3[2]
#               +41 lightbar_setup
#               +42 led_brightness
#               +43 player_leds
#               +44..46 lightbar RGB
# Byte 50..73 : reserved[24]
# Byte 74..77 : crc32 LE

VALID_FLAG0_SPEAKER_VOLUME_ENABLE = 1 << 5
VALID_FLAG0_MIC_VOLUME_ENABLE     = 1 << 6
VALID_FLAG0_AUDIO_CONTROL_ENABLE  = 1 << 7
VALID_FLAG1_MIC_MUTE_LED_CTRL_EN  = 1 << 0
VALID_FLAG1_POWER_SAVE_CTRL_EN    = 1 << 1
VALID_FLAG1_LIGHTBAR_CTRL_EN      = 1 << 2
VALID_FLAG1_RELEASE_LEDS          = 1 << 3
VALID_FLAG1_PLAYER_LED_CTRL_EN    = 1 << 4
VALID_FLAG1_AUDIO_CONTROL2_ENABLE = 1 << 7
VALID_FLAG2_LIGHTBAR_SETUP_CTRL   = 1 << 1

# audio_control Bits 4-5 = OUTPUT_PATH_SEL (verifiziert aus USB-Skript)
# 0b00 (0x00) = L+R -> Kopfhoerer, Speaker MUTE (Sony-Default)
# 0b01 (0x10) = L   -> Kopfhoerer, Speaker MUTE
# 0b10 (0x20) = L   -> Kopfhoerer, R -> Speaker
# 0b11 (0x30) = Kopfhoerer MUTE, R -> Speaker (PURER SPEAKER)
PATH_AUTO       = 0 << 4   # = 0x00 Sony-Default
PATH_HEADPHONE  = 1 << 4   # = 0x10 nur Kopfhoerer
PATH_BOTH       = 2 << 4   # = 0x20 beides
PATH_SPEAKER    = 3 << 4   # = 0x30 PURER SPEAKER

# Speaker-Volume: PS5-FW akzeptiert NUR 0x3D..0x64 (61..100).
# Werte darueber uebersteuern den eingebauten Speaker-Verstaerker massiv.
SPEAKER_VOL_MIN = 0x3D
SPEAKER_VOL_MAX = 0x64

# lightbar_setup
LIGHTBAR_LIGHT_OUT = 1 << 1   # gesetzt = LED AUS, ungesetzt = LED AN

def ps5_crc32(data: bytes) -> int:
    """Sony's BT-Report-CRC. Standard CRC-32 (IEEE) mit Seed-Byte 0xA2 davor."""
    return zlib.crc32(bytes([0xA2]) + data) & 0xFFFFFFFF

def build_setup_report(speaker_vol=SPEAKER_VOL_MAX, preamp=0x00, path=PATH_SPEAKER,
                       led_rgb=(0x00, 0x40, 0xFF), led_brightness=0x02,
                       player_leds=0x04, full_flags=False) -> bytes:
    """Report 0x31 mit Speaker-On, Volume max, Path forciert + LED an.

    full_flags=True: valid_flag0 = 0xFF (alle Enable-Bits, USB-Analogie zu
    pydualsense rep[1]=0xFF). Test ob ein bisher ungesetztes Enable-Bit den
    Membran-Audio-Pfad scharf macht. Achtung: kann Vibration triggern
    (COMPATIBLE_VIBRATION bit0).
    """
    buf = bytearray(78)
    buf[0] = 0x31
    buf[1] = 0x10          # seq_tag (irgendwas != 0)
    buf[2] = 0x00          # tag

    # common starts at byte 3
    c = 3
    if full_flags:
        buf[c+0] = 0xFF    # alle valid_flag0 enable-bits (USB rep[1] analog)
    else:
        buf[c+0] = (VALID_FLAG0_SPEAKER_VOLUME_ENABLE
                    | VALID_FLAG0_AUDIO_CONTROL_ENABLE)
    buf[c+1] = (VALID_FLAG1_AUDIO_CONTROL2_ENABLE
                | VALID_FLAG1_LIGHTBAR_CTRL_EN
                | VALID_FLAG1_RELEASE_LEDS
                | VALID_FLAG1_PLAYER_LED_CTRL_EN)
    buf[c+2] = 0x00        # motor_right
    buf[c+3] = 0x00        # motor_left
    buf[c+4] = 0x7F        # headphone_volume
    # Hard-Clamp auf safe range; gegen Verstaerker-Uebersteuerung.
    safe_vol = max(0, min(SPEAKER_VOL_MAX, speaker_vol))
    buf[c+5] = safe_vol & 0xFF
    buf[c+6] = 0x00        # mic_volume
    buf[c+7] = path & 0xFF # audio_control: OUTPUT_PATH_SEL
    buf[c+8] = 0x00        # mute_button_led
    buf[c+9] = 0x00        # power_save_control
    # reserved2[27] left zero
    buf[c+37] = preamp & 0x07  # audio_control2: SP_PREAMP_GAIN
    buf[c+38] = VALID_FLAG2_LIGHTBAR_SETUP_CTRL
    buf[c+41] = 0x00        # lightbar_setup (NICHT LIGHT_OUT -> LED an)
    buf[c+42] = led_brightness & 0xFF
    buf[c+43] = player_leds & 0xFF
    buf[c+44] = led_rgb[0] & 0xFF
    buf[c+45] = led_rgb[1] & 0xFF
    buf[c+46] = led_rgb[2] & 0xFF

    crc = ps5_crc32(bytes(buf[:74]))
    struct.pack_into("<I", buf, 74, crc)
    return bytes(buf)

def build_release_report() -> bytes:
    """Speaker-Routing aufheben (Path=AUTO), Stream-Buffer freigeben, LED gruen."""
    return build_setup_report(speaker_vol=SPEAKER_VOL_MIN, preamp=0x00, path=PATH_AUTO,
                              led_rgb=(0x00, 0xFF, 0x00))

# --- Report 0x32 (141 byte total, Container fuer Sub-Packets) ----------------
# Byte 0     : report_id = 0x32
# Byte 1     : [tag:4 | seq:4]
# Byte 2.. : Sub-Packet-Array, jedes Sub-Packet:
#              Byte 0: [pid:6 | unk:1 | sized:1]
#              Byte 1: length
#              Byte 2..: data[length]
# Byte 138..141 : crc32 LE
#
# Total 142 byte: report_id (1) + HID-Datafields (141) — HID-Descriptor sagt
# Report Count = 0x8d = 141 fuer Report 0x32.
#
# SAxense schickt zwei Sub-Packets:
#   1) {pid=0x11, sized=1, length=7, data=[0xFE,0,0,0,0,0xFF,COUNTER]}
#   2) {pid=0x12, sized=1, length=64, data=PCM} <-- Haptik
#
# Wir tauschen 0x12 -> $PID und PCM-Sample-Groesse/-Format

REPORT_32_SIZE    = 142
REPORT_32_CRC_OFF = REPORT_32_SIZE - 4   # CRC sitzt @ byte 138..141

def build_audio_report(seq: int, pid: int, sample_bytes: bytes, counter: int,
                       init_mask: int = 0xFE, init_byte5: int = 0xFF,
                       pid_unk: int = 0, init_route: int = 0) -> bytes:
    """Report 0x32 mit Init-Packet (0x11) + Audio-Packet (gewaehlte PID).

    Wichtige Stellschrauben (Sony-undokumentiert, raten):
      - init_mask: 1. Byte der 0x11-data. SAxense: 0xFE (= 11111110).
                   Vermutung Channel-Enable-Bitmaske. Bit 0 koennte Speaker sein:
                     0xFE (SAxense default, Haptik)
                     0xFF (alles an)
                     0x01 (nur "Bit 0", spec. Speaker?)
                     0x03 / 0x05 ... weitere Versuche
      - init_byte5: 6. Byte der 0x11-data. SAxense: 0xFF. Vielleicht zweite Mask.
      - pid_unk: das mysterioese Bit 6 im Sub-Packet-Header. SAxense default 0.
                 unk=1 koennte "Decoder-Modus"/"komprimiert" markieren.
      - init_route: Byte 1 der 0x11-data (SAxense=0, ununtersucht). Verdacht:
                    Routing/Kanal-Selektor Haptik(0) vs Speaker-Membran(?).
    """
    buf = bytearray(REPORT_32_SIZE)
    buf[0] = 0x32
    buf[1] = ((seq & 0x0F) << 4) | 0x00  # seq:4 hi, tag:4 lo

    # Sub-Packet 0x11 (Init/Container-Header), length=7
    # data = [init_mask, init_route, 0, 0, 0, init_byte5, counter]
    off = 2
    buf[off+0] = (0x11 & 0x3F) | (1 << 7)   # pid=0x11, sized=1, unk=0
    buf[off+1] = 7
    buf[off+2:off+9] = bytes([init_mask & 0xFF, init_route & 0xFF, 0x00, 0x00,
                              0x00, init_byte5 & 0xFF, counter & 0xFF])
    off += 2 + 7

    # Sub-Packet $pid (Audio), length=len(sample_bytes)
    sample_size = len(sample_bytes)
    buf[off+0] = ((pid & 0x3F)
                  | ((pid_unk & 1) << 6)   # <-- unk bit
                  | (1 << 7))              # sized=1
    buf[off+1] = sample_size & 0xFF
    buf[off+2:off+2+sample_size] = sample_bytes
    off += 2 + sample_size

    # rest already 0

    crc = ps5_crc32(bytes(buf[:REPORT_32_CRC_OFF]))
    struct.pack_into("<I", buf, REPORT_32_CRC_OFF, crc)
    return bytes(buf)

# --- Main ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="DualSense BT speaker lucky-shot")
    ap.add_argument("hidraw", help="z.B. /dev/hidraw5")
    ap.add_argument("--pid", type=lambda s: int(s, 0), default=0x12,
                    help="Sub-Packet-PID. 0x12 = Sony's Audio-PID (sowohl Haptik als "
                         "auch Speaker, Routing per Sample-Rate)")
    ap.add_argument("--sample-size", type=int, default=64,
                    help="PCM-Bytes pro Report. 64 = 8 ms bei 8 kHz u8 mono.")
    ap.add_argument("--rate", type=int, default=6000,
                    help="Sample-Rate Hz. 6000 = Gold-Wert (durchgaengiger Ton). "
                         "3000 stereo = Haptik (SAxense)")
    ap.add_argument("--channels", type=int, default=1, help="1=mono (Speaker), 2=stereo (Haptik)")
    ap.add_argument("--bytes-per-sample", type=int, default=1, choices=[1, 2],
                    help="1=8-bit (Speaker + Haptik), 2=16-bit")
    ap.add_argument("--unsigned", action="store_true",
                    help="PCM ist unsigned (u8). Default: signed (s8) — Gold-Wert. "
                         "Bestimmt das Silence-Sample (s8->0x00, u8->0x80).")
    ap.add_argument("--setup-only", action="store_true",
                    help="Nur Setup-Report schicken, kein Audio. Zum LED/Volume-Check.")
    ap.add_argument("--release-only", action="store_true",
                    help="Notfall: Speaker-Routing aufheben + LED gruen. Stoppt knatternde FW.")
    ap.add_argument("--no-setup", action="store_true",
                    help="Setup-Report ueberspringen (wenn schon eingerichtet).")
    ap.add_argument("--speaker-vol", type=lambda s: int(s, 0), default=0x52,
                    help="Speaker-Volume 0x3D..0x64. Gold-Wert 0x52. Darueber "
                         "uebersteuert der eingebaute Verstaerker.")
    ap.add_argument("--preamp", type=lambda s: int(s, 0), default=0x00,
                    help="audio_control2 SP_PREAMP_GAIN 0..7. PS5-Default = 0.")
    ap.add_argument("--path", choices=["auto", "speaker", "headphone", "both"],
                    default="auto",
                    help="OUTPUT_PATH_SEL. Gold-Wert 'auto' (0x00) — bei BT der "
                         "funktionierende Pfad.")
    ap.add_argument("--init-mask", type=lambda s: int(s, 0), default=0xFE,
                    help="1. Byte im 0x11-Init-Packet. NUR 0xFE ist sicher — "
                         "0xFF killt die BT-Verbindung (Neu-Pairing noetig).")
    ap.add_argument("--init-byte5", type=lambda s: int(s, 0), default=0xFF,
                    help="6. Byte im 0x11-Init-Packet. SAxense=0xFF.")
    ap.add_argument("--init-route", type=lambda s: int(s, 0), default=0,
                    help="2. Byte im 0x11-Init-Packet (SAxense=0, ununtersucht). "
                         "Routing-Verdacht Haptik vs Speaker-Membran.")
    ap.add_argument("--full-flags", action="store_true",
                    help="valid_flag0 = 0xFF im Setup (USB-Analogie pydualsense "
                         "rep[1]=0xFF). Test ob ungesetztes Enable-Bit Membran oeffnet.")
    ap.add_argument("--unk", type=int, default=0, choices=[0, 1],
                    help="Sub-Packet-Header 'unk'-Bit. SAxense=0. =1 koennte "
                         "anderer Decoder-Pfad sein.")
    ap.add_argument("--keepalive-sec", type=float, default=0.5,
                    help="Periodisch 0x31-Setup-Report nachschicken (Sekunden), "
                         "sonst faellt die FW nach ~20s in Haptik-Default zurueck. "
                         "0 = aus (z.B. wenn das TUI eigene Keep-Alives schickt).")
    args = ap.parse_args()

    # Sicherheits-Guard: PID != 0x12 kann Reports als Keyboard-Input ins
    # System leiten (virtuelle Tastatur oeffnet sich). Nur 0x12 ist Audio.
    if args.pid != 0x12:
        print(f"[warn] PID 0x{args.pid:02x} != 0x12 — andere PIDs koennen als "
              f"Keyboard-Input interpretiert werden. Fortfahren auf eigene Gefahr.",
              file=sys.stderr)

    # Sicherheits-Guard: init_mask 0xFF killt die BT-Verbindung (Neu-Pairing).
    if args.init_mask == 0xFF:
        print("[abort] --init-mask 0xFF killt die BT-Verbindung. Abgebrochen. "
              "Nutze 0xFE.", file=sys.stderr)
        return 2

    path_val = {"auto": PATH_AUTO, "speaker": PATH_SPEAKER,
                "headphone": PATH_HEADPHONE, "both": PATH_BOTH}[args.path]

    fd = os.open(args.hidraw, os.O_WRONLY)
    try:
        if args.release_only:
            os.write(fd, build_release_report())
            print("[release] path=auto + LED gruen gesendet", file=sys.stderr)
            return 0
        if not args.no_setup:
            setup = build_setup_report(speaker_vol=args.speaker_vol,
                                       preamp=args.preamp, path=path_val,
                                       full_flags=args.full_flags)
            os.write(fd, setup)
            print(f"[setup] 0x31 sent (path={args.path}, vol=0x{args.speaker_vol:02x},"
                  f" preamp={args.preamp})", file=sys.stderr)
            time.sleep(0.05)
        if args.setup_only:
            return 0

        # Frame timing: jedes Report-32 traegt $sample_size bytes
        # = $sample_size / (channels * bytes_per_sample) Audio-Frames
        # bei $rate Hz -> Periode
        frames_per_packet = args.sample_size // (args.channels * args.bytes_per_sample)
        period = frames_per_packet / args.rate
        fmt_desc = f"{args.bytes_per_sample}B-{'unsigned' if args.unsigned else 'signed'}"
        print(f"[stream] pid=0x{args.pid:02x} sample_size={args.sample_size}"
              f" rate={args.rate} ch={args.channels} fmt={fmt_desc}"
              f" -> {1000*period:.2f} ms/packet, {1/period:.1f} packets/s", file=sys.stderr)

        # Silence-Sample: signed (s8/s16) = 0x00, unsigned-8 (u8) = 0x80.
        if args.bytes_per_sample == 1 and args.unsigned:
            silence_byte = 0x80
        else:
            silence_byte = 0x00
        silence_chunk = bytes([silence_byte]) * args.sample_size

        # Keep-Alive: 0x31-Setup-Report alle keepalive_sec mitschicken, sonst
        # faellt die FW nach ~20s in Haptik-Default (Rotoren an!).
        ka_setup = build_setup_report(speaker_vol=args.speaker_vol,
                                      preamp=args.preamp, path=path_val,
                                      full_flags=args.full_flags)
        last_ka = time.monotonic()

        counter = 0
        seq = 0
        next_t = time.monotonic()
        stopped_normally = False
        while True:
            chunk = sys.stdin.buffer.read(args.sample_size)
            if not chunk:
                stopped_normally = True
                break
            if len(chunk) < args.sample_size:
                chunk = chunk + (bytes([silence_byte]) * (args.sample_size - len(chunk)))
            pkt = build_audio_report(seq, args.pid, chunk, counter,
                                     init_mask=args.init_mask,
                                     init_byte5=args.init_byte5,
                                     pid_unk=args.unk,
                                     init_route=args.init_route)
            try:
                os.write(fd, pkt)
            except OSError as e:
                print(f"[err] write: {e}", file=sys.stderr)
                break
            counter = (counter + 1) & 0xFF
            seq = (seq + 1) & 0x0F

            # Keep-Alive einschieben
            now = time.monotonic()
            if args.keepalive_sec > 0 and now - last_ka >= args.keepalive_sec:
                try:
                    os.write(fd, ka_setup)
                except OSError:
                    pass
                last_ka = now

            next_t += period
            slack = next_t - time.monotonic()
            if slack > 0:
                time.sleep(slack)
            else:
                next_t = time.monotonic()   # resync, kein Stau-Up

        # Silence-Tail: ~150ms Stille einfuettern, damit Sony's BT-FW
        # nicht den letzten Audio-Buffer im Loop weiterspielt.
        silence_packets = max(10, int(0.15 / period))
        for _ in range(silence_packets):
            pkt = build_audio_report(seq, args.pid, silence_chunk, counter)
            try:
                os.write(fd, pkt)
            except OSError:
                break
            counter = (counter + 1) & 0xFF
            seq = (seq + 1) & 0x0F
            time.sleep(period)

        # Release: Path zurueck auf AUTO, Volume runter, LED gruen
        try:
            os.write(fd, build_release_report())
        except OSError:
            pass
        print("[stop] silence-tail + release report sent", file=sys.stderr)
    finally:
        os.close(fd)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
