#!/bin/bash
# ds5-usb-speaker.sh — DualSense speaker over USB (wrapper for the systemd service).
#
#   ./ds5-usb-speaker.sh start   # set up routing + keep the HID speaker-enable alive
#   ./ds5-usb-speaker.sh stop     # tear everything down
#
# Over USB the DualSense already exposes a 4-channel USB-audio card; we only
# need to (a) route FL/FR to the membrane (RL/RR are haptics, muted to avoid
# buzz) and (b) keep sending the HID output report that un-mutes the speaker.
#
# Requires: pydualsense (pip install --user pydualsense)

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

case "${1:-}" in
  start)
    # 1) PipeWire routing: surround-40 profile + FL/FR-only sink
    bash "$DIR/ds5-usb-setup.sh"
    # 2) HID speaker-enable, keep-alive in the foreground (so systemd tracks it)
    exec python3 "$DIR/ds5_usb_enable.py" 100
    ;;
  stop)
    pkill -f "ds5_usb_enable.py" 2>/dev/null || true
    bash "$DIR/ds5-usb-setup.sh" stop || true
    echo "USB speaker stopped."
    ;;
  *)
    echo "Usage: $0 {start|stop}"; exit 1 ;;
esac
