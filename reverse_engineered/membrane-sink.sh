#!/bin/bash
# membrane-sink.sh — DualSense-Membran als auswaehlbares Ubuntu-Audiogeraet.
#
#   ./membrane-sink.sh start   # Sink "DualSense BT Speaker" anlegen + Stream
#   ./membrane-sink.sh stop    # Stream beenden + Sink entfernen
#
# Danach erscheint im Ubuntu-Sound-Menue ein Ausgabegeraet "DualSense BT
# Speaker" mit eigenem Lautstaerkeregler. Audio das dorthin geht wird per
# Opus an den Controller gestreamt (Report 0x36, DS5_Bridge-Format).

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MEMBRANE="$SCRIPT_DIR/ps5bt_membrane.py"
SINK_NAME="DualSense_BT_Speaker"
SINK_DESC="DualSense BT Speaker"
PIDFILE="/tmp/ds_membrane_stream.pid"
MODFILE="/tmp/ds_membrane_sink.mod"

find_hidraw() {
  for d in /sys/class/hidraw/hidraw*; do
    [ -e "$d/device/uevent" ] || continue
    if grep -q "HID_ID=0005:0000054C:00000CE6" "$d/device/uevent" 2>/dev/null; then
      echo "/dev/$(basename "$d")"; return 0
    fi
  done
  return 1
}

case "${1:-}" in
  start)
    HIDRAW="$(find_hidraw)" || { echo "DualSense BT nicht verbunden."; exit 1; }
    echo "DualSense: $HIDRAW"

    # 1) Null-Sink anlegen (erscheint als Ubuntu-Audiogeraet)
    if ! pactl list short sinks | grep -q "$SINK_NAME"; then
      MOD=$(pactl load-module module-null-sink \
              sink_name="$SINK_NAME" \
              rate=48000 channels=2 \
              sink_properties="device.description='$SINK_DESC'")
      echo "$MOD" > "$MODFILE"
      echo "Sink '$SINK_DESC' angelegt (Modul $MOD)"
    else
      echo "Sink existiert bereits."
    fi

    # 2) Stream starten: Sink-Monitor -> Opus -> Controller
    #    parec liest das (lautstaerke-angepasste) Monitor-Signal des Sinks.
    parec --device="${SINK_NAME}.monitor" --rate=48000 --channels=2 \
          --format=s16le --latency-msec=15 --raw \
      | sudo "$MEMBRANE" "$HIDRAW" &
    echo $! > "$PIDFILE"

    echo ""
    echo "===================================================================="
    echo "Fertig. Im Ubuntu-Sound-Menue jetzt '$SINK_DESC' als Ausgabe waehlen."
    echo "Lautstaerke ueber den normalen Regler. Stop: ./membrane-sink.sh stop"
    echo "===================================================================="
    ;;

  stop)
    # Stream-Pipeline beenden
    if [ -f "$PIDFILE" ]; then
      kill "$(cat "$PIDFILE")" 2>/dev/null || true
      rm -f "$PIDFILE"
    fi
    pkill -f "ps5bt_membrane.py" 2>/dev/null || true
    sudo pkill -f "ps5bt_membrane.py" 2>/dev/null || true
    pkill -f "parec --device=${SINK_NAME}.monitor" 2>/dev/null || true
    sleep 0.3
    # Sink entfernen
    if [ -f "$MODFILE" ]; then
      pactl unload-module "$(cat "$MODFILE")" 2>/dev/null || true
      rm -f "$MODFILE"
    else
      # Fallback: per Name finden
      for mid in $(pactl list short modules | grep "sink_name=$SINK_NAME" | awk '{print $1}'); do
        pactl unload-module "$mid" 2>/dev/null || true
      done
    fi
    # Controller-Speaker freigeben
    "$SCRIPT_DIR/ps5bt_speaker.py" "$(find_hidraw)" --release-only 2>/dev/null || true
    echo "Gestoppt, Sink entfernt."
    ;;

  *)
    echo "Nutzung: $0 {start|stop}"
    exit 1
    ;;
esac
