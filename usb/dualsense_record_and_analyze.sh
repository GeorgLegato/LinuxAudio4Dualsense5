#!/bin/bash
# Nimmt 6 Sekunden vom DualSense-Mikrofon auf während Speaker spielt.
# Generiert: WAV, Spektrogramm-PNG, Waveform-PNG, FFT-Peak-Text.
#
# Workflow:
#   1. Audio läuft schon (Spiel/Browser im Hintergrund)
#   2. Dieses Skript starten
#   3. 6 s warten — Mikrofon nimmt den Speaker auf
#   4. PNG-Dateien an Claude schicken

set -e
OUT=/tmp/ds_recording
mkdir -p "$OUT"
MIC="alsa_input.usb-Sony_Interactive_Entertainment_DualSense_Wireless_Controller-00.iec958-stereo.4"
DUR=6

echo "Audio sollte schon laufen. Aufnahme startet jetzt für ${DUR} s..."
parec --device="$MIC" --file-format=wav --rate=48000 --channels=2 \
      "$OUT/mic.wav" &
RECPID=$!
sleep "$DUR"
kill "$RECPID" 2>/dev/null
wait 2>/dev/null
echo "Aufnahme fertig: $OUT/mic.wav ($(stat -c%s $OUT/mic.wav) bytes)"

echo
echo "Generiere Spektrogramm..."
ffmpeg -y -i "$OUT/mic.wav" \
       -lavfi "showspectrumpic=s=1600x600:legend=1:scale=log:mode=combined:color=intensity" \
       "$OUT/spectrum.png" 2>/dev/null

echo "Generiere Waveform..."
ffmpeg -y -i "$OUT/mic.wav" \
       -filter_complex "showwavespic=s=1600x300:colors=cyan|magenta" \
       "$OUT/waveform.png" 2>/dev/null

echo "FFT-Analyse (Top-Energie-Bänder)..."
# Average loudness per 1/3-octave band via ebur128-ähnliche Lese
ffmpeg -y -i "$OUT/mic.wav" \
       -af "highpass=f=50,lowpass=f=20000,astats=metadata=1:reset=1" \
       -f null - 2>&1 | grep -E "RMS level|Peak level|Bit depth|Dynamic range" | head -10 \
       | tee "$OUT/stats.txt"

echo
echo "=== Output ==="
ls -la "$OUT"/
echo
echo "Schicke an Claude:"
echo "   1. $OUT/spectrum.png  (Frequenz-vs-Zeit-Heatmap)"
echo "   2. $OUT/waveform.png  (Pegel-vs-Zeit)"
echo "   3. $OUT/stats.txt     (RMS/Peak/etc.)"
