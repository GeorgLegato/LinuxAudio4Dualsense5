# Handheld mode — stream to a phone, play on the DualSense 🎮📱

Turn your PC + a phone + a DualSense into a **DIY PlayStation Portal**, all
wireless: the **game's video** is streamed to the phone, while **input, sound,
rumble and the lightbar all stay on the DualSense** (via the rest of this
project). On Linux you even get something the real Portal can't — game audio out
of the controller's **own speaker / headphone jack**.

```
   PC (game) ──video──▶ phone (Moonlight)         "the screen"
        │
        └──audio──▶ DualSense BT Speaker          this project
   DualSense ──input/BT──▶ PC                      "the gamepad"
```

## Pieces

| Role | What | Cost |
|------|------|------|
| Video host (PC) | **Sunshine** (LizardByte, open source) | free |
| Video client (phone) | **Moonlight** (App Store / Play Store, open source) | free |
| Sound + input + haptics + LEDs | **this project** + the DualSense over Bluetooth | — |

> For Steam games you can skip Sunshine and use **Steam Link** (phone) + Steam's
> built-in **Remote Play** (PC) instead — same idea, zero extra host install.

## Setup

```bash
cd src && make moonlight        # installs Sunshine (user flatpak) + prints steps
```

Then, as the script explains:

1. Start Sunshine, open **https://localhost:47990**, add your game/Desktop.
2. Set the PC audio output to **"DualSense BT Speaker"** and **mute the stream
   audio in Moonlight** → game sound plays on the controller, not the phone.
3. Pair the **DualSense to the PC** over Bluetooth (local input — you hold it).
4. Open **Moonlight** on the phone, pair with the PIN, stream.

Wi-Fi (5 GHz) is plenty; for a wired, no-Wi-Fi link you can use the phone's
USB tethering and point Moonlight at the PC's address on that interface.

## Notes

- Sunshine is a separate project (GPLv3); `make moonlight` just installs the
  Flathub build as a **`--user` flatpak** (stays in your home, survives SteamOS
  updates) and documents how to compose it with the DualSense audio here.
- The phone is a passive display: the game runs on the PC and responds to the
  DualSense paired to the PC. The phone shows the stream.
