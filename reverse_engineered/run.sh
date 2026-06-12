#!/bin/bash
# run.sh — DualSense BT Speaker Lucky-Shot Wrapper.
#
# Beispiele:
#   ./run.sh tone 0x13                  # 440 Hz Sinus durch PID 0x13
#   ./run.sh sweep                      # alle PIDs 0x10..0x18 durchprobieren (Sinus, 3s je)
#   ./run.sh pipe 0x13 16000            # 16 kHz mono u8 vom PipeWire-Default-Sink
#   ./run.sh setup                      # nur Setup-Report
#
# Vorausgesetzt: DualSense ist als BT-HID gepairt + verbunden, /dev/hidraw* mit uaccess

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$SCRIPT_DIR/ps5bt_speaker.py"

find_hidraw() {
  # Sucht den DualSense BT hidraw node (Vendor 054C, Product 0CE6, uhid path)
  for d in /sys/class/hidraw/hidraw*; do
    [ -e "$d/device/uevent" ] || continue
    if grep -q "HID_ID=0005:0000054C:00000CE6" "$d/device/uevent" 2>/dev/null; then
      echo "/dev/$(basename "$d")"
      return 0
    fi
  done
  return 1
}

HIDRAW="$(find_hidraw)" || { echo "DualSense BT hidraw nicht gefunden."; exit 1; }
echo "Using $HIDRAW"

case "${1:-}" in
  setup)
    "$PY" "$HIDRAW" --setup-only --path speaker --speaker-vol 0xFF --preamp 0x07
    ;;

  release)
    # Notausgang: stoppt knatternde FW, LED gruen
    "$PY" "$HIDRAW" --release-only
    ;;

  # ===== MEMBRAN-Speaker (Opus, DS5_Bridge-Format) =====
  membrane-tone)
    # 440 Hz Sinus auf die echte Membran (Opus). -re = Echtzeit-Lieferung,
    # damit der Reader gleichmaessig gefuettert wird (BT-Jitter-Isolations-Test).
    DUR="${2:-5}"
    HZ="${3:-440}"
    MEMBRANE="$SCRIPT_DIR/ps5bt_membrane.py"
    echo "Membran-Ton $HZ Hz, ${DUR}s (Opus 48k stereo -> report 0x36, echtzeit)"
    trap 'kill %1 2>/dev/null' INT
    ffmpeg -hide_banner -loglevel error -re \
      -f lavfi -i "sine=frequency=$HZ:duration=$DUR" \
      -ac 2 -ar 48000 -f s16le - \
      | sudo "$MEMBRANE" "$HIDRAW" --diag
    ;;

  membrane)
    # Live-Musik vom Default-Sink auf die echte Membran (Opus).
    #   ./run.sh membrane          -> mit sudo (Echtzeit-Prio gegen Haker)
    #   ./run.sh membrane nosudo   -> ohne sudo (Timing per busy-wait)
    #   ./run.sh membrane unbind   -> zusaetzlich hid-playstation loesen
    MEMBRANE="$SCRIPT_DIR/ps5bt_membrane.py"
    DEFAULT_SINK="$(pactl get-default-sink)"
    MON="${DEFAULT_SINK}.monitor"
    EXTRA=""
    RUN="sudo"
    case "${2:-}" in
      nosudo) RUN="" ;;
      unbind) EXTRA="--unbind" ;;
    esac
    echo "Membran-Stream${EXTRA:+ (unbind)}: $MON -> Opus 48k stereo -> 0x36"
    parec --device="$MON" --rate=48000 --channels=2 --format=s16le --latency-msec=15 --raw \
      | $RUN "$MEMBRANE" "$HIDRAW" $EXTRA
    ;;

  membrane-diag)
    # Wie membrane, aber mit Diagnose-Ausgabe (write-Latenz, underruns)
    MEMBRANE="$SCRIPT_DIR/ps5bt_membrane.py"
    DEFAULT_SINK="$(pactl get-default-sink)"
    MON="${DEFAULT_SINK}.monitor"
    echo "Membran-DIAG: $MON -> Opus -> 0x36 (1s-Statistik)"
    parec --device="$MON" --rate=48000 --channels=2 --format=s16le --latency-msec=15 --raw \
      | sudo "$MEMBRANE" "$HIDRAW" --diag
    ;;

  membrane-trim)
    # Clock-Drift-Trim: 440Hz-Dauerton mit waehlbarer Sende-Periode (us).
    # Bei 0.5s-Hakern verschiedene Werte probieren bis der Ton glatt ist:
    #   ./run.sh membrane-trim 10000   (Default, 100/s)
    #   ./run.sh membrane-trim 10050   (langsamer -> gegen Overflow)
    #   ./run.sh membrane-trim 9950    (schneller -> gegen Underflow)
    PERIOD_US="${2:-10000}"
    DUR="${3:-15}"
    HZ="${4:-440}"
    MEMBRANE="$SCRIPT_DIR/ps5bt_membrane.py"
    echo "Membran-Trim: period=${PERIOD_US}us, ${HZ}Hz, ${DUR}s"
    trap 'kill %1 2>/dev/null' INT
    ffmpeg -hide_banner -loglevel error -re \
      -f lavfi -i "sine=frequency=$HZ:duration=$DUR" \
      -ac 2 -ar 48000 -f s16le - \
      | sudo "$MEMBRANE" "$HIDRAW" --period-us "$PERIOD_US"
    ;;

  tone)
    PID="${2:-0x12}"
    RATE="${3:-6000}"     # GOLD-WERT
    FMT="${4:-s8}"        # GOLD-WERT: s8
    DUR="${5:-3}"
    VOL_PCT="${6:-2.0}"   # GOLD-WERT
    echo "Tone 440 Hz, PID=$PID, rate=$RATE, fmt=$FMT, dur=${DUR}s, pcm-vol=$VOL_PCT"
    BPS=1
    [ "$FMT" = "s16le" ] || [ "$FMT" = "s16be" ] && BPS=2
    ffmpeg -hide_banner -loglevel error \
      -f lavfi -i "sine=frequency=440:duration=$DUR" \
      -af "volume=$VOL_PCT" \
      -ac 1 -ar "$RATE" -f "$FMT" - \
      | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                         --channels 1 --bytes-per-sample "$BPS" \
                         --sample-size 64
    ;;

  sweep)
    DUR="${2:-3}"
    RATE="${3:-16000}"
    FMT="${4:-u8}"
    PIDS="${5:-0x10 0x13 0x14 0x15 0x16 0x17 0x18}"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for PID in $PIDS; do
      echo
      echo "===================== PID $PID ====================="
      # 1) Markier-Beep: 880Hz aufsteigend 0.4s -- hoerbares "hier kommt PID X"
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=880:duration=0.4" \
        -ac 1 -ar "$RATE" -f "${FMT}" - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                          --channels 1 --bytes-per-sample "$([ "$FMT" = "s16le" ] && echo 2 || echo 1)" \
                          --sample-size 64 || true
      sleep 0.5
      # 2) Eigentlicher Test-Sinus
      echo "  -> 440 Hz $DUR s..."
      "$0" tone "$PID" "$RATE" "$FMT" "$DUR" || true
      sleep 1
    done
    trap - INT
    ;;

  one)
    # Eine einzelne PID gruendlich abklopfen mit Sinus + Speech-Test
    PID="${2:-0x13}"
    RATE="${3:-16000}"
    FMT="${4:-u8}"
    BPS=1
    [ "$FMT" = "s16le" ] && BPS=2
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for HZ in 200 440 800 1500 3000; do
      echo "  $HZ Hz 1.5s..."
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=$HZ:duration=1.5" \
        -ac 1 -ar "$RATE" -f "$FMT" - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                          --channels 1 --bytes-per-sample "$BPS" \
                          --sample-size 64 || true
      sleep 0.3
    done
    trap - INT
    ;;

  # mask-sweep: PID fix (0x12), variiert Init-Mask im 0x11-Packet.
  # Vermutung: SAxense's 0xFE blockiert Speaker-Bit. Wir testen 4 Kandidaten.
  mask-sweep)
    PID="${2:-0x12}"
    RATE="${3:-16000}"
    FMT="${4:-s16le}"
    BPS=1
    [ "$FMT" = "s16le" ] && BPS=2
    DUR="${5:-2}"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for MASK in 0xFE 0xFF 0x01 0x03 0x05 0x07 0x0F 0x80; do
      echo
      echo "=== PID=$PID MASK=$MASK FMT=$FMT @ ${RATE} Hz ==="
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=440:duration=$DUR" \
        -ac 1 -ar "$RATE" -f "$FMT" - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                          --channels 1 --bytes-per-sample "$BPS" \
                          --sample-size 64 \
                          --init-mask "$MASK" || true
      sleep 1
    done
    trap - INT
    ;;

  # fmt-sweep: PID + MASK fix, variiert Format / Rate.
  fmt-sweep)
    PID="${2:-0x12}"
    MASK="${3:-0xFE}"
    DUR="${4:-2}"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for COMBO in "u8:8000:1" "u8:16000:1" "s16le:8000:2" "s16le:16000:2" "s16le:24000:2" "s16le:48000:2"; do
      IFS=":" read -r FMT RATE BPS <<<"$COMBO"
      echo
      echo "=== PID=$PID MASK=$MASK FMT=$FMT @ ${RATE} Hz ==="
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=440:duration=$DUR" \
        -ac 1 -ar "$RATE" -f "$FMT" - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                          --channels 1 --bytes-per-sample "$BPS" \
                          --sample-size 64 \
                          --init-mask "$MASK" || true
      sleep 1
    done
    trap - INT
    ;;

  # codec-sweep: PID + Mask + Rate fix, variiert das Audio-Encoding.
  # Sony's BT-Codec ist undokumentiert — testet die wahrscheinlichen Kandidaten.
  # WICHTIG: --bytes-per-sample 1 fuer alle 1-byte-codecs; ffmpeg macht die Wandlung.
  codec-sweep)
    RATE="${2:-8000}"
    DUR="${3:-3}"
    PID="${4:-0x12}"
    MASK="${5:-0xFE}"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    # Codec-Combos: "ffmpeg-format:bytes-per-sample:beschreibung"
    # ALLE mit Anti-Clipping-Volume 0.5, weil USB-Erfahrung zeigt Sony's DAC
    # ab ~76% Aussteuerung zerlegt
    for COMBO in \
        "u8:1:unsigned-u8" \
        "s8:1:signed-s8" \
        "mulaw:1:mu-law-Telefon" \
        "alaw:1:A-law-Telefon" \
        "s16le:2:linear-s16le" \
        "s16be:2:linear-s16-big-endian" ; do
      IFS=":" read -r FFMT BPS DESC <<<"$COMBO"
      echo
      echo "=== codec=$DESC @ ${RATE} Hz mono PID=$PID MASK=$MASK ==="
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=440:duration=$DUR" \
        -af "volume=0.5" \
        -ac 1 -ar "$RATE" -f "$FFMT" - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                          --channels 1 --bytes-per-sample "$BPS" \
                          --sample-size 64 \
                          --init-mask "$MASK" || true
      sleep 1
    done
    trap - INT
    ;;

  # vol-sweep: s8 8kHz fest, variiert die PCM-Amplitude
  # s8 ist VERIFIZIERT als das saubere Format. Jetzt: welche Lautstaerke
  # bleibt clean ohne wieder zu clippen?
  vol-sweep)
    DUR="${2:-3}"
    FFMT="${3:-s8}"   # s8 nach Erkenntnis aus codec-sweep
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for VOL in 0.3 0.5 0.7 0.85 1.0; do
      echo
      echo "=== $FFMT mono 8kHz PCM-vol=$VOL ==="
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=440:duration=$DUR" \
        -af "volume=$VOL" \
        -ac 1 -ar 8000 -f "$FFMT" - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid 0x12 --rate 8000 \
                          --channels 1 --bytes-per-sample 1 \
                          --sample-size 64 \
                          --init-mask 0xFE || true
      sleep 1
    done
    trap - INT
    ;;

  # preamp-sweep: SP_PREAMP_GAIN 0..3 mit s8 fest, fester PCM-Vol.
  # USB-Doku sagt "PS5 uses 0-2". Vielleicht ist 1 oder 2 die richtige Gain-Stufe.
  preamp-sweep)
    DUR="${2:-3}"
    VOL="${3:-0.85}"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for PRE in 0 1 2 3 4 7; do
      echo
      echo "=== s8 mono 8kHz vol=$VOL preamp=$PRE ==="
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=440:duration=$DUR" \
        -af "volume=$VOL" \
        -ac 1 -ar 8000 -f s8 - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid 0x12 --rate 8000 \
                          --channels 1 --bytes-per-sample 1 \
                          --sample-size 64 \
                          --init-mask 0xFE \
                          --preamp "$PRE" || true
      sleep 1
    done
    trap - INT
    ;;

  # size-sweep: variiert Sample-Size pro Paket. Vielleicht erwartet die FW
  # 32 byte / 96 byte / 128 byte Audio-Frames statt 64.
  size-sweep)
    RATE="${2:-8000}"
    FMT="${3:-u8}"
    DUR="${4:-3}"
    PID="${5:-0x12}"
    MASK="${6:-0xFE}"
    BPS=1
    [ "$FMT" = "s16le" ] && BPS=2
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for SIZE in 32 48 64 80 96 112 120; do
      MS=$(awk "BEGIN{printf \"%.2f\", 1000*$SIZE/($RATE*$BPS)}")
      echo
      echo "=== sample_size=$SIZE byte = ${MS} ms/paket @ ${RATE} Hz $FMT ==="
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=440:duration=$DUR" \
        -ac 1 -ar "$RATE" -f "$FMT" - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                          --channels 1 --bytes-per-sample "$BPS" \
                          --sample-size "$SIZE" \
                          --init-mask "$MASK" || true
      sleep 1
    done
    trap - INT
    ;;

  # rate-sweep: u8 mono PID 0x12, variiert Sample-Rate um den 8-kHz-Sweet-Spot
  # Sucht max Sample-Rate, bei der die Membran sauber bleibt.
  rate-sweep)
    DUR="${2:-2}"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for RATE in 4000 6000 8000 10000 12000 16000 20000 24000; do
      echo
      echo "=== u8 mono PID=0x12 MASK=0xFE @ ${RATE} Hz ==="
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=440:duration=$DUR" \
        -ac 1 -ar "$RATE" -f u8 - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid 0x12 --rate "$RATE" \
                          --channels 1 --bytes-per-sample 1 \
                          --sample-size 64 \
                          --init-mask 0xFE || true
      sleep 1
    done
    trap - INT
    ;;

  # speech: TTS-artiger Test mit Sprach-aehnlichem Material (8 kHz)
  speech)
    DUR="${2:-5}"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    # Sweep 200 Hz -> 2000 Hz mit Amplituden-Modulation, klingt am Speaker
    # ueberzeugend wenn der Pfad sauber ist
    ffmpeg -hide_banner -loglevel error \
      -f lavfi -i "sine=frequency=200:duration=$DUR" \
      -af "aeval=sin(2*PI*(200+1800*t/$DUR)*t)*sin(2*PI*5*t)" \
      -ac 1 -ar 8000 -f u8 - 2>/dev/null \
      | "$PY" "$HIDRAW" --pid 0x12 --rate 8000 \
                        --channels 1 --bytes-per-sample 1 \
                        --sample-size 64 \
                        --init-mask 0xFE
    trap - INT
    ;;

  # unk-sweep: testet pid_unk-Bit-Toggle
  unk-sweep)
    PID="${2:-0x12}"
    MASK="${3:-0xFE}"
    RATE="${4:-16000}"
    FMT="${5:-s16le}"
    BPS=1
    [ "$FMT" = "s16le" ] && BPS=2
    DUR="${6:-2}"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    for UNK in 0 1; do
      echo
      echo "=== PID=$PID MASK=$MASK UNK=$UNK FMT=$FMT @ ${RATE} Hz ==="
      ffmpeg -hide_banner -loglevel error \
        -f lavfi -i "sine=frequency=440:duration=$DUR" \
        -ac 1 -ar "$RATE" -f "$FMT" - 2>/dev/null \
        | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                          --channels 1 --bytes-per-sample "$BPS" \
                          --sample-size 64 \
                          --init-mask "$MASK" --unk "$UNK" || true
      sleep 1
    done
    trap - INT
    ;;

  pipe)
    # Audio vom PipeWire-Default-Sink an die DualSense, durch ffmpeg fuer
    # Format-Wandlung. Erlaubt jeden Codec, den ffmpeg ausspuckt.
    # WICHTIG: USB-Erfahrung sagt Speaker-DAC clipped ab ~76% Aussteuerung
    # -> wir reduzieren PCM-Volume auf VOL_PCT (default 0.5 = 50%).
    PID="${2:-0x12}"
    RATE="${3:-6000}"   # GOLD-WERT
    FFMT="${4:-s8}"     # GOLD-WERT: s8
    VOL_PCT="${5:-2.0}" # GOLD-WERT
    BPS=1
    [ "$FFMT" = "s16le" ] || [ "$FFMT" = "s16be" ] && BPS=2
    DEFAULT_SINK="$(pactl get-default-sink)"
    MON="${DEFAULT_SINK}.monitor"
    echo "Streaming $MON via ffmpeg vol=$VOL_PCT -> $FFMT @ ${RATE}Hz mono -> PID $PID"
    trap '"$PY" "$HIDRAW" --release-only 2>/dev/null; exit 130' INT
    parec --device="$MON" --rate=48000 --channels=2 --format=s16le --latency-msec=15 --raw \
      | ffmpeg -hide_banner -loglevel error \
               -f s16le -ar 48000 -ac 2 -i - \
               -af "volume=$VOL_PCT" \
               -ac 1 -ar "$RATE" -f "$FFMT" - 2>/dev/null \
      | "$PY" "$HIDRAW" --pid "$PID" --rate "$RATE" \
                         --channels 1 --bytes-per-sample "$BPS" \
                         --sample-size 64
    trap - INT
    ;;

  *)
    cat <<EOF
Nutzung:
  $0 setup
  $0 release                                # Notausgang: stoppt FW-Knattern, LED gruen
  $0 tone       [PID] [RATE] [FMT] [DUR_S] [VOL]  # VOL=0..1 (Default 0.5, Anti-Clipping)
  $0 sweep      [DUR_S] [RATE] [FMT]        # PIDs 0x10..0x18 durchprobieren
  $0 one        [PID] [RATE] [FMT]          # Eine PID, 5 Frequenzen
  $0 pipe       [PID] [RATE] [FMT] [VOL]    # Default-Sink-Monitor live durchpipen (VOL Default 0.5)
  $0 mask-sweep [PID] [RATE] [FMT] [DUR_S]  # PID fix, variiert 0x11-Init-Mask
  $0 fmt-sweep  [PID] [MASK] [DUR_S]        # PID+MASK fix, variiert Format/Rate
  $0 rate-sweep [DUR_S]                     # u8 mono PID 0x12, Rate-Variationen
  $0 speech     [DUR_S]                     # Sweep 200->2000 Hz, Sprach-Test
  $0 unk-sweep  [PID] [MASK] [RATE] [FMT]   # unk-Bit togglen
  $0 codec-sweep [RATE] [DUR_S]             # u8 / s8 / mu-law / a-law / s16le / s16be (Vol 0.5)
  $0 vol-sweep  [DUR_S] [FMT]               # s8 8kHz fest, variiert PCM-Vol 0.3..1.0
  $0 preamp-sweep [DUR_S] [VOL]             # s8 8kHz vol fest, SP_PREAMP_GAIN 0..7
  $0 size-sweep  [RATE] [FMT] [DUR_S]       # Sample-Size 32/48/64/80/96/112/120

VERIFIZIERTE Speaker-Config: PID=0x12, MASK=0xFE, u8 mono @ 8000 Hz
Defaults (pipe): PID=0x12, RATE=8000, FMT=u8
EOF
    exit 1
    ;;
esac
