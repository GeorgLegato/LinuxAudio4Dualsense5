#!/usr/bin/env bash
# setup-moonlight.sh — turn this PC into a handheld game-streaming host:
# install Sunshine (the open-source host for the Moonlight app) and print the
# steps to stream a PC game to your phone while sound + input stay on the
# DualSense. Companion to this project (DualSense BT audio).
#
# Sunshine is third-party (LizardByte, GPLv3). We install the Flathub build as a
# --user flatpak, so it lives in your home and survives SteamOS updates — no root.
set -euo pipefail

APP=dev.lizardbyte.app.Sunshine

echo "== Handheld setup: Sunshine (host) for Moonlight (phone) =="

if ! command -v flatpak >/dev/null 2>&1; then
    cat >&2 <<EOF
Flatpak is required but not found.
  SteamOS / Steam Deck: it's already there (it's how Discover installs apps).
  Ubuntu/Debian:        sudo apt install flatpak
Then re-run this script.
EOF
    exit 1
fi

echo "-> ensuring Flathub remote (user)"
flatpak remote-add --if-not-exists --user flathub https://flathub.org/repo/flathub.flatpakrepo

echo "-> installing Sunshine ($APP) into your user flatpak (no root)"
flatpak install -y --user flathub "$APP"

echo "-> running Sunshine's additional input/permission setup"
flatpak run --command=additional-install.sh "$APP" || \
    echo "  (additional-install reported an issue — usually fine; re-run if input is missing)"

cat <<EOF

============================================================================
Sunshine is installed. Now wire up the "handheld" — game on phone, everything
else on the DualSense:

1. START the host:
     flatpak run $APP
   (or enable autostart from your desktop's autostart settings)

2. CONFIGURE it once — open the web UI in a browser:
     https://localhost:47990
   set a username/password, then under "Applications" add "Desktop" or your game.

3. AUDIO on the DualSense (the whole point):
   - Make sure THIS project is installed and the controller is paired, then set
     the system audio output to  "DualSense BT Speaker".
   - In the Moonlight app on the phone, MUTE the stream audio (or phone volume 0).
   -> game sound (incl. our subwoofer / headphone jack) plays on the controller,
      not on the phone.

4. INPUT on the DualSense:
   - Pair the DualSense to THIS PC over Bluetooth (local input). You hold the
     controller and play; the phone is just the screen.

5. PHONE: open Moonlight, it finds this PC, enter the PIN it shows back in the
   Sunshine web UI to pair. Start streaming.

That's your DIY PlayStation Portal: screen on the phone, sound + input + rumble
+ lightbar on the DualSense — over plain Wi-Fi (5 GHz recommended).
============================================================================
EOF
