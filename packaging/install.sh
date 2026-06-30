#!/usr/bin/env bash
# Install DualSense BT Speaker from a prebuilt release — no root, no compiler.
#
# Designed for immutable / read-only distros like SteamOS (Steam Deck): it only
# writes into your HOME (~/.local/bin + a systemd --user service), which survives
# OS updates. Works the same on any PipeWire-based Linux.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/ds5_membrane_sink"
SVC="$HERE/ds5-membrane-sink.service"
DEST="$HOME/.local/bin"
SVCDIR="$HOME/.config/systemd/user"

[ -f "$BIN" ] || { echo "error: ds5_membrane_sink not found next to this script." >&2; exit 1; }

echo "== DualSense BT Speaker installer =="

# Runtime libraries the binary needs (SteamOS ships all of these).
missing=""
for lib in libpipewire-0.3.so.0 libopus.so.0 libz.so.1; do
    if ! { ldconfig -p 2>/dev/null | grep -q "$lib"; } && ! ldd "$BIN" 2>/dev/null | grep -q "$lib.*=> /"; then
        missing="$missing $lib"
    fi
done
if [ -n "$missing" ]; then
    echo "WARNING: these runtime libraries look missing:$missing"
    echo "         Install PipeWire + Opus and re-run. (On SteamOS they are present.)"
fi

mkdir -p "$DEST" "$SVCDIR"
install -Dm755 "$BIN" "$DEST/ds5_membrane_sink"
install -Dm644 "$SVC" "$SVCDIR/ds5-membrane-sink.service"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable --now ds5-membrane-sink.service || true
    echo "Service enabled (auto-starts on login)."
else
    echo "No systemd --user found. Run manually: $DEST/ds5_membrane_sink --wait"
fi

cat <<EOF

Done — installed into ~/.local (survives OS updates).

  1. Pair your DualSense over Bluetooth (it shows up as a gamepad).
  2. Pick "DualSense BT Speaker" in your sound settings and play anything.
  3. Tweak it in the browser:  http://localhost:8118
     (Rumble-as-Subwoofer, headphone jack, mic, LED analyser — all live.)

Status:     systemctl --user status  ds5-membrane-sink
Uninstall:  ./uninstall.sh
EOF
