# Reverse-engineering tools

These are the scripts used to crack the DualSense Bluetooth speaker protocol.
They are kept for reference, reproducibility, and further experimentation. For
normal use, prefer the native PipeWire sink in [`../src`](../src).

Everything here is Python 3 + `ffmpeg` + `libopus` (via `ctypes`), talking
directly to `/dev/hidraw` of the Bluetooth-paired controller.

## Files

| File | Purpose |
|------|---------|
| `ps5bt_membrane.py` | Reference implementation of the **speaker** path: Opus CBR, report `0x36`, 512→480 resample, 10.667 ms pacing, fade-in, mic-mute, input-grab. The native C node is a port of this. |
| `ps5bt_speaker.py` | Earlier **haptics**-path experiments (raw PCM into sub-packet `0x12`, report `0x32`). How the project started before the speaker path was found. |
| `tune.py` | Interactive curses tuner — live-toggle every protocol parameter (PID, format, rate, masks, routing) while listening. The tool that mapped the unknown fields. |
| `run.sh` | Test harness: tone/sweep/diagnostic modes, `membrane-tone`, `membrane-diag`, format/rate/codec sweeps. |
| `membrane-sink.sh` | Pre-native-node integration: a PulseAudio/PipeWire **null-sink** + `parec` pipe into `ps5bt_membrane.py`. Superseded by `../src` but still works. |

## Quick reference

```bash
# Play a clean test tone on the membrane (proves the speaker path)
./run.sh membrane-tone 20 800

# Live music via a selectable null-sink device
./membrane-sink.sh start      # creates "DualSense BT Speaker"
./membrane-sink.sh stop

# Diagnostics (send rate, underruns, queue length)
./run.sh membrane-diag
```

## What was figured out here

1. The speaker is **Opus**, not raw PCM — report `0x36`, sub-packet `0x13`.
2. **CBR 160 kbps** gives the fixed 200-byte frames the firmware expects.
3. The frame interval is **512/48000 = 10.667 ms**, not 10 ms — the cause of the
   ~0.5 s periodic stutter (clock mismatch / buffer overrun), found via `btmon`.
4. The controller returns a **microphone stream** when the speaker is active;
   it must be muted in the state snapshot or it floods the BT link.
5. CRC-32 seed `0xA2`; no application-layer crypto — plain `hidraw` writes work.

See the top-level [README](../README.md) for the full protocol table and credits.
