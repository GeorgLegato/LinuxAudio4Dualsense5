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
| Microphone | muted in the state snapshot, or the controller floods the BT link with a return stream |

The single most important detail is the **10.667 ms frame interval**: sending at
the obvious 10 ms is 6.67 % too fast and overruns the controller's audio buffer,
producing a periodic ~0.5 s stutter. Resampling 512→480 and pacing at
512/48000 s matches the controller's audio clock exactly.

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

## Repository layout

```
src/                    native PipeWire sink (the real product)
  ds5_membrane_sink.c   ~250 lines of C: PipeWire node + Opus + 0x36 + CRC
  Makefile
reverse_engineered/     the tools that cracked the protocol (see its README)
  ps5bt_membrane.py     Python reference implementation of the speaker path
  ps5bt_speaker.py      the earlier haptics-path experiments
  tune.py               interactive curses tuner used during RE
  run.sh                test harness (tones, sweeps, diagnostics)
  membrane-sink.sh      null-sink + parec workaround (pre-native-node)
```

## Status

- ✅ Membrane speaker over Bluetooth, smooth, full range (verified to 2 kHz+)
- ✅ Native PipeWire sink, selectable device, volume control
- ✅ Microphone muted, indicator LED quiet, no input-event interference
- ⚠️ A short start-up transient (speaker amp power-on pop) may remain

## Credits

This work stands entirely on two open-source projects that reverse-engineered
the DualSense Bluetooth audio path first:

- **[DS5_Bridge](https://github.com/SundayMoments/DS5_Bridge)** (AGPL-3.0) — the
  Raspberry Pi Pico bridge whose `audio.cpp`/`bt.cpp` revealed the `0x36` report
  format, the Opus CBR parameters, the 512/480 timing and the speaker-enable
  sequence. This project is a native-Linux reimplementation of that protocol
  knowledge.
- **[SAxense](https://github.com/egormanga/SAxense)** (MPL-2.0) — the original
  proof that DualSense haptics audio can be driven over Bluetooth from
  `/dev/hidraw`, and the source of the report-container layout and CRC seed.

## License

**GNU AGPL-3.0** (see [LICENSE](LICENSE)) — chosen for compatibility with
DS5_Bridge, whose protocol analysis this project builds on.

## Legal

Reverse engineering for interoperability is permitted under EU Directive
2009/24/EC Art. 6 and recognised in the US (*Sega v. Accolade*). This project
contains **no Sony code**; an undocumented protocol format is functional fact,
not a copyrightable work. "DualSense" and "PlayStation" are trademarks of Sony
Interactive Entertainment, used here only for identification.
