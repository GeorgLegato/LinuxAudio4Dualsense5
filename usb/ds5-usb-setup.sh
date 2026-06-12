#!/bin/bash
# DualSense-Speaker — komplettes Pipeline-Setup nach Reboot/Reconnect.
#
# Nutzung:
#   bash ~/dualsense-speaker-setup.sh        # Setup, dann HID-Hack starten
#   bash ~/dualsense-speaker-setup.sh stop   # Alles wieder abreissen
#
# Was es macht:
#   1) Wartet bis DualSense USB sichtbar (max 15s)
#   2) Card-Profile auf 4ch-Surround setzen
#   3) Alte DS_* virtuelle Sinks abreissen (idempotent)
#   4) DS_Speaker  = FL/FR-only Routing (RL/RR=mute, kein Haptic-Brumm)
#   5) DS_MonoR    = Mono-Mix L+R vor DS_Speaker
#   6) Default-Sink + Volumes setzen
#   7) Hinweis ausgeben, dass HID-Hack noch separat zu starten ist

set -e

CARD="alsa_card.usb-Sony_Interactive_Entertainment_DualSense_Wireless_Controller-00"
HW_SINK_PREFIX="alsa_output.usb-Sony_Interactive_Entertainment_DualSense_Wireless_Controller-00.analog-surround-40"

# Resolve actual sink name (PipeWire may add .N suffix after profile-switch)
resolve_hw_sink() {
  pactl list short sinks 2>/dev/null \
    | awk '{print $2}' \
    | grep -E "^${HW_SINK_PREFIX}(\\.[0-9]+)?$" \
    | head -n1
}

cleanup_sinks() {
  for n in DS_MonoR DS_Speaker DS_LP DS_LIM DS_16k DS_24k DS_CMP \
           DS_Headphones DualSense_Speaker DualSense_Headphones DualSense_Headphones_FL; do
    for mid in $(pactl list short modules 2>/dev/null | grep -E "sink_name=$n|source=$n" | awk '{print $1}'); do
      pactl unload-module "$mid" 2>/dev/null || true
    done
  done
}

if [ "$1" = "stop" ]; then
  echo "Stopping DualSense speaker pipeline..."
  pkill -f "ds5_usb_enable.py" 2>/dev/null || true
  pkill -f "dualsense_tune.py" 2>/dev/null || true
  sleep 1
  cleanup_sinks
  echo "Done. Speaker is back to Sony default (muted)."
  exit 0
fi

# Wait for DualSense
echo "Waiting for DualSense controller..."
for i in $(seq 1 15); do
  if pactl list cards short 2>/dev/null | grep -q "$CARD"; then
    break
  fi
  sleep 1
done
if ! pactl list cards short 2>/dev/null | grep -q "$CARD"; then
  echo "ERROR: DualSense card not found. Connect via USB."
  exit 1
fi

# Card profile
echo "Setting card profile..."
pactl set-card-profile "$CARD" "output:analog-surround-40+input:iec958-stereo" || true
sleep 1

# Clean old sinks
cleanup_sinks
sleep 0.3

# Resolve hardware sink (might be .analog-surround-40 or .analog-surround-40.N)
HW_SINK="$(resolve_hw_sink)"
for i in $(seq 1 10); do
  [ -n "$HW_SINK" ] && break
  sleep 0.5
  HW_SINK="$(resolve_hw_sink)"
done
if [ -z "$HW_SINK" ]; then
  echo "ERROR: surround-40 hardware sink did not appear."
  exit 1
fi
echo "Using HW sink: $HW_SINK"

# DS_Speaker = FL/FR-only (RL/RR null -> no haptic)
echo "Loading DS_Speaker (FL/FR-only)..."
pactl load-module module-remap-sink \
  sink_name=DS_Speaker \
  sink_master="$HW_SINK" \
  channels=2 \
  master_channel_map=front-left,front-right \
  channel_map=front-left,front-right \
  remix=true \
  >/dev/null

# DS_MonoR = Mono-Mix
echo "Loading DS_MonoR (Mono-Mix)..."
pactl load-module module-remap-sink \
  sink_name=DS_MonoR \
  sink_master=DS_Speaker \
  channels=2 \
  master_channel_map=front-left,front-right \
  channel_map=mono,mono \
  remix=true \
  >/dev/null

# Default + Volume
pactl set-default-sink DS_MonoR
pactl set-sink-volume DS_MonoR 100%
pactl set-sink-volume DS_Speaker 100%
pactl set-sink-volume "$HW_SINK" 100%

echo ""
echo "===================================================================="
echo "DualSense PipeWire pipeline ready:"
echo "  App -> DS_MonoR -> DS_Speaker -> surround-40 -> DualSense"
echo ""
echo "NEXT STEP: Start the HID-Hack-Script (in another terminal):"
echo "  /usr/bin/python3 /home/alex/ds5_usb_enable.py 100"
echo ""
echo "Or for interactive tuning:"
echo "  /usr/bin/python3 ds5_usb_tune.py"
echo "===================================================================="
