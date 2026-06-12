#!/usr/bin/env python3
# ps5bt_membrane.py — DualSense MEMBRAN-Speaker ueber Bluetooth (Opus).
#
# Das ist der ECHTE Speaker-Pfad (nicht Haptik). Reverse-engineered aus
# DS5_Bridge (SundayMoments) src/audio.cpp + bt.cpp.
#
# Schluessel-Erkenntnis: Membran-Audio geht NICHT als rohes PCM, sondern
# als OPUS-codierte Frames in Report 0x36, Sub-Packet 0x13.
#
# Report 0x36 (398 byte), vier Sub-Packets:
#   [2]   0x11 config  (mask 0xFF, 5x buffer_length=64, counter)
#   [11]  0x10 state    (63-byte Controller-State-Snapshot)
#   [76]  0x12 haptik   (64 byte int8 PCM, hier Stille)
#   [142] 0x13 speaker  (200 byte: Opus-Frame, 480 samples @ 48kHz stereo)
#   KEIN CRC (DS5_Bridge sendet ohne)
#
# Opus: opus_encoder_create(48000, 2, OPUS_APPLICATION_AUDIO), 480 frames/Paket.
#
# Input: 48000 Hz, 2ch, s16le von stdin (z.B. via ffmpeg).

import os
import sys
import glob
import time
import zlib
import fcntl
import queue
import ctypes
import select
import struct
import threading
import argparse

def try_realtime_priority():
    """SCHED_FIFO fuer praezises 10ms-Timing (best-effort, braucht rtprio/root)."""
    try:
        param = os.sched_param(20)
        os.sched_setscheduler(0, os.SCHED_FIFO, param)
        return True
    except (PermissionError, OSError, AttributeError):
        return False

# EVIOCGRAB: Input-Event-Device exklusiv grabben, damit Steam/GNOME die
# Phantom-Gamepad-Events waehrend des Audio-Streams NICHT sehen.
EVIOCGRAB = 0x40044590

def grab_controller_inputs(hidraw_path):
    """Finde + grabbe alle /dev/input/eventX des Controllers (vom hidraw aus).

    Devices werden O_NONBLOCK geoeffnet, damit ein Drain-Thread die Phantom-
    Events leerlesen kann (sonst Stau -> 10s-Nachlauf beim Stop)."""
    grabbed = []
    node = os.path.basename(hidraw_path)            # hidraw12
    hid_dev = f"/sys/class/hidraw/{node}/device"
    event_dirs = glob.glob(f"{hid_dev}/input/input*/event*")
    for ev in event_dirs:
        evname = os.path.basename(ev)               # eventN
        devpath = f"/dev/input/{evname}"
        try:
            fd = os.open(devpath, os.O_RDWR | os.O_NONBLOCK)
            fcntl.ioctl(fd, EVIOCGRAB, 1)
            grabbed.append((fd, devpath))
        except OSError as e:
            print(f"[warn] grab {devpath}: {e}", file=sys.stderr)
    return grabbed

def drain_inputs_loop(grabbed, stop_flag):
    """Leert die gegrabbten Event-Queues via select (GIL-schonend)."""
    fds = [fd for fd, _ in grabbed]
    while not stop_flag.is_set():
        try:
            ready, _, _ = select.select(fds, [], [], 0.2)
        except OSError:
            return
        for fd in ready:
            try:
                while os.read(fd, 4096):
                    pass
            except (BlockingIOError, OSError):
                pass

def release_controller_inputs(grabbed):
    for fd, devpath in grabbed:
        try:
            # letzte Reste leerlesen, dann freigeben
            try:
                while os.read(fd, 4096):
                    pass
            except (BlockingIOError, OSError):
                pass
            fcntl.ioctl(fd, EVIOCGRAB, 0)
            os.close(fd)
        except OSError:
            pass

# --- hid-playstation Treiber unbind/rebind (gegen Phantom-Input + Bandbreite) -

def hid_device_id(hidraw_path):
    """HID-Device-ID (z.B. 0005:0000054C:00000CE6.0018) aus dem hidraw-Pfad."""
    node = os.path.basename(hidraw_path)
    try:
        link = os.readlink(f"/sys/class/hidraw/{node}/device")
        # .../uhid/0005:054C:0CE6.0018/hidraw/hidraw12 -> wir wollen den .NNNN-Teil
        # device-symlink zeigt auf das hid-device-Verzeichnis
        return os.path.basename(os.path.dirname(
            os.path.dirname(os.path.realpath(f"/sys/class/hidraw/{node}/device"))))
    except OSError:
        return None

def hid_unbind(hidraw_path):
    """hid-playstation vom Controller loesen. Gibt die device-id zurueck (rebind)."""
    node = os.path.basename(hidraw_path)
    real = os.path.realpath(f"/sys/class/hidraw/{node}/device")
    # real = .../0005:054C:0CE6.0018/hidrawXX/device -> hid-id ist ein Pfadteil
    hid_id = None
    for part in real.split("/"):
        if part.count(":") == 2 and "." in part:
            hid_id = part
    if not hid_id:
        return None
    try:
        with open("/sys/bus/hid/drivers/playstation/unbind", "w") as f:
            f.write(hid_id)
        return hid_id
    except OSError as e:
        print(f"[warn] hid unbind ({hid_id}): {e} — root noetig", file=sys.stderr)
        return None

def hid_rebind(hid_id):
    if not hid_id:
        return
    try:
        with open("/sys/bus/hid/drivers/playstation/bind", "w") as f:
            f.write(hid_id)
    except OSError:
        pass

def find_dualsense_hidraw():
    """Aktuelle DualSense-hidraw finden (Nummer aendert sich nach unbind/reconnect)."""
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(f"{d}/device/uevent") as f:
                if "HID_ID=0005:0000054C:00000CE6" in f.read():
                    return f"/dev/{os.path.basename(d)}"
        except OSError:
            continue
    return None

def ps5_crc32(data: bytes) -> int:
    """DualSense BT-Report-CRC. crc32_seeded(data, 0xEADA2D49) == zlib mit
    0xA2-Prefix (bt.cpp build_interrupt_output_packet + fill_output_report_checksum)."""
    return zlib.crc32(bytes([0xA2]) + data) & 0xFFFFFFFF

# --- libopus via ctypes -------------------------------------------------------

OPUS_APPLICATION_AUDIO = 2049
OPUS_SET_BITRATE_REQUEST    = 4002
OPUS_SET_VBR_REQUEST        = 4006
OPUS_SET_COMPLEXITY_REQUEST = 4010
OPUS_SET_EXPERT_FRAME_DURATION_REQUEST = 4040
OPUS_FRAMESIZE_10_MS = 5003
# DS5_Bridge-Werte: CBR 160kbps @ 10ms = EXAKT 200 byte/frame (= opus_buf-Groesse)
DS5_BITRATE = 200 * 8 * 100   # 160000

def load_opus():
    for name in ("libopus.so.0", "libopus.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise RuntimeError("libopus nicht gefunden (apt install libopus0)")

class OpusEncoder:
    def __init__(self, rate=48000, channels=2, bitrate=DS5_BITRATE):
        self.lib = load_opus()
        self.lib.opus_encoder_create.restype = ctypes.c_void_p
        self.lib.opus_encoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int)]
        self.lib.opus_encode.restype = ctypes.c_int32
        self.lib.opus_encode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32]
        self.lib.opus_encoder_ctl.restype = ctypes.c_int
        # WICHTIG: argtypes setzen, sonst wird der 64-bit-Encoder-Pointer
        # auf 32 bit truncated -> Segfault. ctl nimmt (enc, request, int-value).
        self.lib.opus_encoder_ctl.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int32]
        err = ctypes.c_int(0)
        self.enc = self.lib.opus_encoder_create(rate, channels,
                                                OPUS_APPLICATION_AUDIO,
                                                ctypes.byref(err))
        if not self.enc or err.value != 0:
            raise RuntimeError(f"opus_encoder_create failed: {err.value}")
        # EXAKT die DS5_Bridge-Encoder-Konfiguration (audio.cpp core1_entry):
        self._ctl(OPUS_SET_EXPERT_FRAME_DURATION_REQUEST, OPUS_FRAMESIZE_10_MS)
        self._ctl(OPUS_SET_BITRATE_REQUEST, bitrate)
        self._ctl(OPUS_SET_VBR_REQUEST, 0)          # CBR -> feste 200 byte/frame
        self._ctl(OPUS_SET_COMPLEXITY_REQUEST, 0)
        self.channels = channels

    def _ctl(self, request, value):
        self.lib.opus_encoder_ctl(self.enc, request, ctypes.c_int32(value))

    def encode(self, pcm_bytes, frame_size):
        """pcm_bytes: int16 interleaved, frame_size = samples per channel."""
        n_int16 = frame_size * self.channels
        buf = (ctypes.c_int16 * n_int16).from_buffer_copy(pcm_bytes)
        out = (ctypes.c_ubyte * 4000)()
        n = self.lib.opus_encode(self.enc, buf, frame_size, out, 4000)
        if n < 0:
            raise RuntimeError(f"opus_encode error {n}")
        return bytes(out[:n])

# --- Report 0x31 Speaker-Enable (aus bt.cpp send_speaker_output_state) --------

VALID_FLAG0_SPEAKER_VOLUME_ENABLE = 0x20
VALID_FLAG0_AUDIO_CONTROL_ENABLE  = 0x80
VALID_FLAG1_AUDIO_CONTROL2_ENABLE = 0x80
AUDIO_PATH_SPEAKER  = 0x30
SPEAKER_VOLUME_MAX  = 0x64
SPEAKER_PREAMP_GAIN = 0x02

def build_speaker_setup() -> bytes:
    """0x31 Report: Speaker-Route enablen (exakt wie DS5_Bridge bt.cpp)."""
    buf = bytearray(78)
    buf[0] = 0x31
    buf[1] = 0x10
    c = 3
    buf[c+0] = VALID_FLAG0_AUDIO_CONTROL_ENABLE | VALID_FLAG0_SPEAKER_VOLUME_ENABLE
    buf[c+1] = VALID_FLAG1_AUDIO_CONTROL2_ENABLE
    buf[c+5]  = SPEAKER_VOLUME_MAX      # speaker_volume
    buf[c+7]  = AUDIO_PATH_SPEAKER      # audio_control = PATH_SPEAKER
    buf[c+37] = SPEAKER_PREAMP_GAIN     # audio_control2 = preamp 2
    return bytes(buf)

# --- Report 0x36 Audio-Container (aus audio.cpp) ------------------------------

REPORT_36_SIZE = 398
SAMPLE_SIZE = 64
AUDIO_SECTION_ENABLE_MASK = 0xFF
HAPTICS_BUFFER_LENGTH = 64
SPEAKER_DATA_SIZE = 200      # pkt[143] = 200 (sizeof opus_buf)
STATE_SNAPSHOT_SIZE = 63

# Controller-State-Snapshot. Basis = DS5_Bridge-Default, ABER MIKROFON HART AUS,
# damit der Controller KEINEN Mic-Audio-Stream zurueckschickt (btmon-Befund:
# Mic-Duplex flutete den BT-Link -> Haker). Aenderungen ggue. DS5_Bridge:
#   byte6 mic_volume:     0xff -> 0x00
#   byte9 power_save_ctrl: 0x00 -> 0x10 (kPowerSaveControlMicMute)
STATE_SNAPSHOT = bytes([
    0xfd, 0xe3, 0x00, 0x00,
    0x7f, 0x64,
    0x00, 0x09, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00,   # [6]mic_vol=0, [9]power_save=mic_mute
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x0a,
    0x04, 0x00, 0x00, 0x00, 0x01,
    0x00,
    0x00, 0x00, 0xff,
] + [0x00] * (STATE_SNAPSHOT_SIZE - 47))   # auf 63 byte mit 0 auffuellen

def build_0x36(seq: int, counter: int, opus_frame: bytes,
               haptik: bytes = b"\x00" * SAMPLE_SIZE,
               headset: bool = False) -> bytes:
    """Report 0x36 mit config/state/haptik/opus-speaker Sub-Packets."""
    pkt = bytearray(REPORT_36_SIZE)
    pkt[0] = 0x36
    pkt[1] = (seq & 0x0F) << 4

    # Sub-Packet 0x11 (config)
    pkt[2] = 0x11 | (1 << 7)
    pkt[3] = 7
    pkt[4] = AUDIO_SECTION_ENABLE_MASK
    pkt[5] = HAPTICS_BUFFER_LENGTH
    pkt[6] = HAPTICS_BUFFER_LENGTH
    pkt[7] = HAPTICS_BUFFER_LENGTH
    pkt[8] = HAPTICS_BUFFER_LENGTH
    pkt[9] = HAPTICS_BUFFER_LENGTH
    pkt[10] = counter & 0xFF

    # Sub-Packet 0x10 (state snapshot)
    pkt[11] = 0x10 | (1 << 7)
    pkt[12] = STATE_SNAPSHOT_SIZE
    pkt[13:13 + STATE_SNAPSHOT_SIZE] = STATE_SNAPSHOT

    # Sub-Packet 0x12 (haptik PCM, hier Stille)
    pkt[76] = 0x12 | (1 << 7)
    pkt[77] = SAMPLE_SIZE
    pkt[78:78 + SAMPLE_SIZE] = haptik[:SAMPLE_SIZE].ljust(SAMPLE_SIZE, b"\x00")

    # Sub-Packet 0x13 (speaker, Opus) — 0x16 fuer Headset-Jack
    pkt[142] = (0x16 if headset else 0x13) | (1 << 7)
    pkt[143] = SPEAKER_DATA_SIZE
    frame = opus_frame[:SPEAKER_DATA_SIZE].ljust(SPEAKER_DATA_SIZE, b"\x00")
    pkt[144:144 + SPEAKER_DATA_SIZE] = frame

    # CRC32 (Seed 0xA2) ueber die ersten len-4 byte, in die letzten 4 (LE).
    # bt.cpp fill_output_report_checksum: crc ueber report[0..394], @ [394..397].
    crc = ps5_crc32(bytes(pkt[:REPORT_36_SIZE - 4]))
    struct.pack_into("<I", pkt, REPORT_36_SIZE - 4, crc)
    return bytes(pkt)

# --- Main ---------------------------------------------------------------------

import array

# DS5_Bridge-exaktes Timing: 512 Input-Frames pro Block, auf 480 Opus-Frames
# resampled, Sende-Periode = 512/48000 = 10.667ms (NICHT 10ms!). Das matcht
# die Controller-Audio-Clock; 10ms waeren 6.67% zu schnell -> 0.5s-Overflow-Haker.
INPUT_BLOCK_FRAMES = 512
OPUS_FRAME_SAMPLES = 480            # Opus-Frame nach Resampling
PERIOD = INPUT_BLOCK_FRAMES / 48000.0          # 10.667ms
PCM_BYTES_PER_FRAME = INPUT_BLOCK_FRAMES * 2 * 2  # 512 * 2ch * 2byte = 2048

# Vorberechnete Resample-Indizes (linear, 512 -> 480), pure-python (kein numpy,
# da das Skript unter sudo laeuft und root oft kein numpy hat).
_STEP = (INPUT_BLOCK_FRAMES - 1) / (OPUS_FRAME_SAMPLES - 1)
_RS_IDX = []
for _i in range(OPUS_FRAME_SAMPLES):
    _src = _i * _STEP
    _idx = int(_src)
    _nxt = _idx + 1 if _idx < INPUT_BLOCK_FRAMES - 1 else _idx
    _RS_IDX.append((_idx, _nxt, _src - _idx))

def resample_512_to_480(pcm_bytes):
    """512 stereo-int16-Frames -> 480 (linear, wie DS5_Bridge Program.cs)."""
    a = array.array('h')
    a.frombytes(pcm_bytes)
    out = array.array('h', bytes(OPUS_FRAME_SAMPLES * 2 * 2))
    for i, (idx, nxt, frac) in enumerate(_RS_IDX):
        b = idx << 1; n = nxt << 1; o = i << 1
        l0 = a[b];   out[o]   = l0 + int((a[n]   - l0) * frac)
        r0 = a[b+1]; out[o+1] = r0 + int((a[n+1] - r0) * frac)
    return out.tobytes()

# DS5_Bridge SPEAKER_TRANSITION_FADE_SAMPLES = 1920 (40ms Fade-In am Audio-Start)
FADE_SAMPLES = 1920

def apply_fade_in(pcm_bytes, sample_start):
    """Linearer Fade-In ueber die ersten FADE_SAMPLES (gegen Start-Knacks).
    Gibt (pcm, neuer_sample_start) zurueck; ab FADE_SAMPLES unveraendert."""
    if sample_start >= FADE_SAMPLES:
        return pcm_bytes, sample_start
    a = array.array('h'); a.frombytes(pcm_bytes)
    n = len(a) // 2
    for i in range(n):
        s = sample_start + i
        if s >= FADE_SAMPLES:
            break
        g = s / FADE_SAMPLES
        a[i*2] = int(a[i*2] * g)
        a[i*2+1] = int(a[i*2+1] * g)
    return a.tobytes(), sample_start + n

def main():
    ap = argparse.ArgumentParser(description="DualSense Membran-Speaker via BT (Opus)")
    ap.add_argument("hidraw", help="z.B. /dev/hidraw5")
    ap.add_argument("--bitrate", type=int, default=0,
                    help="Opus-Bitrate (0 = Opus-Default fuer 48k stereo)")
    ap.add_argument("--headset", action="store_true",
                    help="Sub-Packet 0x16 (Klinke) statt 0x13 (interner Speaker)")
    ap.add_argument("--no-setup", action="store_true")
    ap.add_argument("--no-grab", action="store_true",
                    help="Input-Event-Devices NICHT grabben (dann Keyboard-Chaos "
                         "im System moeglich).")
    ap.add_argument("--unbind", action="store_true",
                    help="hid-playstation-Treiber vom Controller loesen "
                         "(behebt Phantom-Input + BT-Bandbreiten-Haker an der "
                         "Wurzel; braucht root, rebind beim Stop).")
    ap.add_argument("--keepalive-sec", type=float, default=0.0,
                    help="Periodischer 0x31-Setup (0=aus). Im Membran-Modus haelt "
                         "der 0x36-Stream den Speaker aktiv; periodischer 0x31 "
                         "kann die BT-Verbindung stoeren.")
    ap.add_argument("--prebuffer", type=int, default=10,
                    help="Jitter-Puffer in Frames (a 10ms) vor Start. 10 = 100ms "
                         "Latenz-Reserve gegen Aussetzer.")
    ap.add_argument("--diag", action="store_true",
                    help="Diagnose: misst os.write-Latenz + Underruns pro Sekunde.")
    ap.add_argument("--period-us", type=int, default=0,
                    help="Sende-Periode in us. 0 = auto (512/48000 = 10667us, "
                         "DS5_Bridge-Rate). Nur fuer Fein-Trim ueberschreiben.")
    args = ap.parse_args()

    if try_realtime_priority():
        print("[rt] SCHED_FIFO aktiv (praezises Timing)", file=sys.stderr)
    else:
        print("[rt] keine Echtzeit-Prio (rtprio/root fehlt) — Timing per sleep",
              file=sys.stderr)

    enc = OpusEncoder(rate=48000, channels=2,
                      bitrate=(args.bitrate or DS5_BITRATE))

    drain_stop = threading.Event()
    drain_thread = None
    grabbed = []
    hid_id = None
    hidraw_path = args.hidraw

    if args.unbind:
        # Wurzel-Fix: Treiber loesen. Achtung: hidraw verschwindet dabei und
        # kommt (sofern ein anderer Treiber bindet) unter neuer Nummer wieder.
        hid_id = hid_unbind(args.hidraw)
        if hid_id:
            print(f"[unbind] hid-playstation von {hid_id} geloest", file=sys.stderr)
            time.sleep(0.4)
            new_path = find_dualsense_hidraw()
            if new_path:
                hidraw_path = new_path
                print(f"[unbind] neue hidraw: {hidraw_path}", file=sys.stderr)
            else:
                print("[unbind] keine hidraw nach unbind — rebind + fallback",
                      file=sys.stderr)
                hid_rebind(hid_id)
                hid_id = None
                time.sleep(0.4)
                hidraw_path = find_dualsense_hidraw() or args.hidraw

    if not hid_id and not args.no_grab:
        grabbed = grab_controller_inputs(hidraw_path)
        if grabbed:
            print(f"[grab] {len(grabbed)} Input-Device(s) gegrabbt + drain "
                  f"(kein Keyboard-Chaos)", file=sys.stderr)
            drain_thread = threading.Thread(
                target=drain_inputs_loop, args=(grabbed, drain_stop),
                daemon=True)
            drain_thread.start()

    fd = os.open(hidraw_path, os.O_WRONLY)
    try:
        if not args.no_setup:
            os.write(fd, build_speaker_setup())
            print("[setup] 0x31 speaker-enable sent", file=sys.stderr)
            time.sleep(0.05)

        print(f"[stream] Opus 48k stereo, 480-frame, report 0x36 "
              f"-> {1000*PERIOD:.1f} ms/packet, Jitter-Puffer glaettet parec-Bursts",
              file=sys.stderr)

        # Reader-Thread absorbiert parecs Bursts (max-gap ~21ms) in eine Queue.
        # Sender sendet GLEICHMAESSIG alle 10ms (sleep, kein busy-wait -> GIL frei).
        # Bei Underrun: letzten Opus-Frame wiederholen (nahtlos statt Stille).
        TARGET = args.prebuffer
        pcm_q = queue.Queue(maxsize=TARGET * 3)
        stop_flag = threading.Event()

        def reader():
            while not stop_flag.is_set():
                chunk = sys.stdin.buffer.read(PCM_BYTES_PER_FRAME)
                if not chunk:
                    stop_flag.set(); return
                if len(chunk) < PCM_BYTES_PER_FRAME:
                    chunk = chunk.ljust(PCM_BYTES_PER_FRAME, b"\x00")
                try:
                    pcm_q.put_nowait(chunk)
                except queue.Full:
                    try:
                        pcm_q.get_nowait(); pcm_q.put_nowait(chunk)
                    except (queue.Empty, queue.Full):
                        pass
        rt = threading.Thread(target=reader, daemon=True); rt.start()

        # Pre-Buffer fuellen
        while pcm_q.qsize() < TARGET and not stop_flag.is_set():
            time.sleep(0.005)

        setup_pkt = build_speaker_setup()
        last_opus = enc.encode(resample_512_to_480(b"\x00" * PCM_BYTES_PER_FRAME),
                               OPUS_FRAME_SAMPLES)
        last_ka = time.monotonic()
        # Default-Periode = DS5_Bridge 512/48000 = 10.667ms; --period-us 0 = auto
        period_s = (args.period_us / 1_000_000.0) if args.period_us else PERIOD
        counter = 0
        seq = 0

        # Silence-Preroll (DS5_Bridge SPEAKER_SILENCE_PREROLL = 24 Pakete): laesst
        # Speaker-Pfad + Opus-Decoder einschwingen, bevor echtes Audio kommt ->
        # kein knacksiger Anfang. 24 * 10.667ms = 256ms.
        silence_opus = enc.encode(resample_512_to_480(b"\x00" * PCM_BYTES_PER_FRAME),
                                  OPUS_FRAME_SAMPLES)
        pre_t = time.monotonic()
        for _ in range(24):
            os.write(fd, build_0x36(seq, counter, silence_opus, headset=args.headset))
            counter = (counter + 1) & 0xFF
            seq = (seq + 1) & 0x0F
            pre_t += period_s
            sl = pre_t - time.monotonic()
            if sl > 0:
                time.sleep(sl)

        d_n = 0; d_rep = 0; d_last = time.monotonic()
        fade_pos = 0
        next_t = time.monotonic()
        while not stop_flag.is_set() or not pcm_q.empty():
            # Drift-/Burst-Management: wenn Puffer zu voll, einen Frame skippen
            # (leert nach parec-Bursts); wenn leer, letzten Frame wiederholen.
            skip = pcm_q.qsize() > TARGET * 2
            try:
                chunk = pcm_q.get_nowait()
                if skip:
                    chunk = pcm_q.get_nowait()   # einen extra ziehen = drop
                resampled = resample_512_to_480(chunk)
                resampled, fade_pos = apply_fade_in(resampled, fade_pos)
                last_opus = enc.encode(resampled, OPUS_FRAME_SAMPLES)
            except queue.Empty:
                d_rep += 1   # Underrun -> last_opus wiederholen

            pkt = build_0x36(seq, counter, last_opus, headset=args.headset)
            try:
                os.write(fd, pkt)
            except OSError as e:
                print(f"[err] write: {e}", file=sys.stderr)
                break
            counter = (counter + 1) & 0xFF
            seq = (seq + 1) & 0x0F

            now = time.monotonic()
            if args.diag:
                d_n += 1
                if now - d_last >= 1.0:
                    print(f"[diag] pkts/s={d_n} repeats={d_rep} "
                          f"qlen={pcm_q.qsize()}", file=sys.stderr)
                    d_n = 0; d_rep = 0; d_last = now

            if args.keepalive_sec > 0 and now - last_ka >= args.keepalive_sec:
                try:
                    os.write(fd, setup_pkt)
                except OSError:
                    pass
                last_ka = now

            # Gleichmaessiges Timing per sleep (GIL-frei fuer den Reader).
            # period_us erlaubt Clock-Drift-Trim gegen Controller-Buffer-Over/Underrun.
            next_t += period_s
            slack = next_t - time.monotonic()
            if slack > 0:
                time.sleep(slack)
            elif slack < -period_s:
                next_t = time.monotonic()   # zu weit hinten -> resync

        print("[stop] stream ended", file=sys.stderr)
    finally:
        os.close(fd)
        drain_stop.set()
        if drain_thread:
            drain_thread.join(timeout=0.5)
        release_controller_inputs(grabbed)
        if hid_id:
            hid_rebind(hid_id)
            print(f"[rebind] hid-playstation an {hid_id}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
