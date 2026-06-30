#!/usr/bin/env bash
# Remove DualSense BT Speaker (the prebuilt install). No root needed.
set -euo pipefail
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now ds5-membrane-sink.service 2>/dev/null || true
fi
rm -f "$HOME/.config/systemd/user/ds5-membrane-sink.service"
rm -f "$HOME/.local/bin/ds5_membrane_sink"
command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload || true
echo "Removed. (Your ~/.DS5/config was left in place — delete it if you want.)"
