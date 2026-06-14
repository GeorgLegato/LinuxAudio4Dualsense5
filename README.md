# DualSense BT Speaker for Linux

**Use the PlayStation 5 DualSense controller's built-in speaker as a regular
audio output device over Bluetooth — on Linux, with no cable.**

Sony's official position is that the DualSense speaker only works over USB.
Over Bluetooth the controller speaks a proprietary, undocumented audio protocol
that no mainstream Linux tool implements. This project does: it registers a
native PipeWire sink called **"DualSense BT Speaker"** that you can select in
your normal sound menu, complete with a volume slider.

```
PipeWire graph ──PCM──▶ ds5_membrane_sink ──Opus/0x36──▶ DualSense (Bluetooth)
```

## How it works

The DualSense speaker over Bluetooth is **not** raw PCM and **not** A2DP. It is
**Opus-encoded audio** wrapped in HID output report `0x36`. The key pieces that
make it work — none of them documented by Sony:

| Piece | Value |
|-------|-------|
| HID output report | `0x36` (398 bytes), four sub-packets: config `0x11`, state `0x10`, haptics `0x12`, **speaker `0x13`** |
| Audio codec | **Opus, 48 kHz stereo, CBR 160 kbps** → exactly 200 bytes/frame |
| Frame timing | **512 input samples → resampled to 480 → sent every 10.667 ms** (512/48000), *not* 10 ms |
| Checksum | CRC-32 with seed `0xA2` over the first 394 bytes |
| Speaker enable | report `0x31`: `audio_control = 0x30` (speaker path), preamp `0x02` |
| Config mask | `0x11` byte 4 = **`0xFE`** — bit 0 *cleared*. See "Audio + gamepad" below. |

Two details matter most:

- **The 10.667 ms frame interval.** Sending at the obvious 10 ms is 6.67 % too
  fast and overruns the controller's audio buffer → periodic ~0.5 s stutter.
  Resampling 512→480 and pacing at 512/48000 s matches the controller's audio
  clock exactly.
- **The config mask `0xFE`, not `0xFF`.** Bit 0 of the audio-section mask turns
  on the controller's **microphone-capture / duplex mode**. With it set, the
  DualSense streams mic audio *back* under the **same HID report id (`0x31`) as
  gamepad input** — which the kernel and Steam misread as full stick deflection
  (phantom input: the avatar runs by itself while audio plays). We only want the
  speaker, so we **clear bit 0** → no mic capture → the input stream stays clean.

## Audio + gamepad at the same time ✔

Because of the `0xFE` mask above, **you can use the controller as a gamepad
*and* hear game audio through it simultaneously over Bluetooth** — no cable, no
filter, no Steam workaround. The controller no longer mixes audio into its input
reports, so Steam Input / the kernel / any mapper see a perfectly normal gamepad.

> If a controller is *already* stuck in duplex from an older session (or another
> app enabled the mic), reconnect it once (PS button) to clear it. The optional
> `mapper/ds5_keymap.py` (stick→WASD straight from hidraw, dropping any stray
> `0xd4` audio frames) is kept as a safety net for that case.

## Quick start

```bash
sudo apt install libpipewire-0.3-dev libopus-dev zlib1g-dev gcc make
cd src
make service
```

That's it. `make service` builds the sink, installs it to `~/.local/bin`, and
enables a **systemd user service** that auto-starts. Pair your DualSense over
Bluetooth (it appears as a gamepad) and **"DualSense BT Speaker"** shows up in
your sound settings — select it and play anything. Volume works through the
normal slider.

The service waits for the controller to connect, brings up the sink
automatically, and restarts itself if the controller drops. It runs in your
normal user session — **no root, no terminal kept open** (the GNOME/systemd way).

```bash
systemctl --user status  ds5-membrane-sink   # is it running?
systemctl --user restart ds5-membrane-sink
make service-uninstall                        # remove it
```

### Run manually instead (for testing)

```bash
cd src
make
./ds5_membrane_sink        # Ctrl+C to stop
```

### Wired? Use the USB variant

If you have no Bluetooth or prefer a cable, there's a USB path too — over USB
the DualSense is a normal 4-channel audio card, it just needs routing + a
speaker-enable. See [`usb/`](usb/):

```bash
pip install --user pydualsense
cd src && make service-usb
```

## Repository layout

```
src/                    native PipeWire sink over BLUETOOTH (the real product)
  ds5_membrane_sink.c   ~250 lines of C: PipeWire node + Opus + 0x36 + CRC
  Makefile              make / make service / make service-usb
  ds5-membrane-sink.service
usb/                    wired variant (4ch USB-audio card + HID enable)
  ds5-usb-speaker.sh    routing + speaker-enable wrapper
  ds5-usb-setup.sh      PipeWire FL/FR routing
  ds5_usb_enable.py     HID speaker-enable keep-alive (needs pydualsense)
mapper/                 optional Steam-free gamepad mapper
  ds5_keymap.py         stick -> WASD straight from hidraw, drops 0xd4 frames
reverse_engineered/     the tools that cracked the BT protocol (see its README)
  ps5bt_membrane.py     Python reference implementation of the speaker path
  ps5bt_speaker.py      the earlier haptics-path experiments
  tune.py               interactive curses tuner used during RE
  init_probe.py         TUI that found the mask bit-0 mic-capture switch
  check_input.py        shows gamepad vs d4-feedback rate from hidraw
  run.sh                test harness (tones, sweeps, diagnostics)
  membrane-sink.sh      null-sink + parec workaround (pre-native-node)
```

## Status

- ✅ Membrane speaker over Bluetooth, smooth, full range (verified to 2 kHz+)
- ✅ Native PipeWire sink, selectable device, volume control
- ✅ Microphone muted, indicator LED quiet, no input-event interference
- ⚠️ A short start-up transient (speaker amp power-on pop) may remain

## Tested with

| Component | Version |
|-----------|---------|
| OS | Ubuntu 24.04.4 LTS (Noble) |
| Kernel | 6.8.0-124-generic |
| Audio | PipeWire 1.0.5 |
| Controller | Sony **DualSense** Wireless Controller, USB ID **`054C:0CE6`** (original model, CFI-ZCT1 series), firmware `0x0110002a` |
| Link | standard Bluetooth adapter (BR/EDR) |

Other distros and kernels should work too — the only hard requirements are
PipeWire and `/dev/hidraw` access — but they are untested. Reports welcome.

## Scope — what this does *not* support

This targets the **PS5 DualSense** specifically. It will **not** work with:

- **DualSense Edge** (USB ID `054C:0DF2`) — different product ID; probably needs
  small tweaks, but untested.
- **DualShock 4** (PS4 controller) — an entirely different device and audio
  protocol. Not supported.
- **Third-party / clone controllers** — these do not implement Sony's audio HID
  reports at all, so there is nothing to talk to.

The protocol details (report `0x36`, Opus parameters, timing) are specific to
the DualSense firmware; a very different firmware revision *could* in principle
behave differently, though none is known to.

## How this was built (and credits)

This project happened in two phases:

1. **Trial and error.** Starting from the insight in
   **[SAxense](https://github.com/egormanga/SAxense)** (MPL-2.0) — that DualSense
   *haptics* audio can be driven over Bluetooth by writing straight to
   `/dev/hidraw` — we got the first crackly sounds out of the controller and
   learned the report-container layout and the CRC-32 seed `0xA2`. An
   interactive tuner (`reverse_engineered/tune.py`) was used to probe every
   unknown field by ear; `btmon` verified each hypothesis on the wire.

2. **Reusing the Pico project for the fine audio details.** The *speaker*
   (membrane) path — Opus encoding, report `0x36`, the CBR parameters, and the
   crucial **512→480 / 10.667 ms** timing that finally killed the periodic
   stutter — was reverse-engineered from
   **[DS5_Bridge](https://github.com/SundayMoments/DS5_Bridge)** (AGPL-3.0-only),
   a **Raspberry Pi Pico 2 W** bridge whose `audio.cpp`/`bt.cpp` document the
   protocol precisely. This project is a native-Linux reimplementation of that
   knowledge — **no Pico, no extra hardware**, just the Bluetooth adapter you
   already have.

DS5_Bridge is itself derived from
**[awalol/DS5Dongle](https://github.com/awalol/DS5Dongle)** (MIT), the original
Pico DualSense dongle. Full chain: DS5Dongle (MIT) → DS5_Bridge (AGPL-3.0) →
this project (AGPL-3.0). Huge thanks to all of them — this stands on their work.

## License

**GNU AGPL-3.0** (see [LICENSE](LICENSE)) — chosen for compatibility with
DS5_Bridge, whose protocol analysis this project builds on.

## Legal

Reverse engineering for interoperability is permitted under EU Directive
2009/24/EC Art. 6 and recognised in the US (*Sega v. Accolade*). This project
contains **no Sony code**; an undocumented protocol format is functional fact,
not a copyrightable work. "DualSense" and "PlayStation" are trademarks of Sony
Interactive Entertainment, used here only for identification.
