# USB variant

For wired use — if you have no Bluetooth, or simply prefer a cable. Over USB the
DualSense is a much simpler case: it already exposes a **4-channel USB-audio
card** that Linux's `snd_usb_audio` picks up automatically. Two things are still
needed:

1. **Routing** — channels FL/FR drive the membrane speaker, RL/RR drive the
   haptic actuators. We route FL/FR to a clean stereo sink and mute RL/RR to
   avoid the haptic buzz (`ds5-usb-setup.sh`).
2. **Speaker enable** — Sony mutes the built-in speaker by default; a HID output
   report un-mutes it and sets the speaker path. Sent as a keep-alive
   (`ds5_usb_enable.py`).

> On Linux 6.17+ the kernel gained native DualSense USB audio-jack handling, so
> some of this may become unnecessary. This variant targets older kernels (and
> the always-mute-by-default behaviour).

## Install

```bash
pip install --user pydualsense        # dependency for the HID enable
cd ../src
make service-usb                      # installs + enables the systemd user service
```

Plug the DualSense in via USB; "DualSense" appears as an output device. Manage:

```bash
systemctl --user status ds5-usb-speaker
make service-usb-uninstall
```

## Manual use

```bash
./ds5-usb-speaker.sh start    # routing + speaker enable (foreground)
./ds5-usb-speaker.sh stop
```

## Files

| File | Purpose |
|------|---------|
| `ds5-usb-speaker.sh` | wrapper used by the service: routing + keep-alive |
| `ds5-usb-setup.sh` | PipeWire routing: surround-40 profile, FL/FR-only sink |
| `ds5_usb_enable.py` | HID speaker-enable keep-alive (needs `pydualsense`) |
| `ds5_usb_tune.py` | interactive tuner used during USB reverse-engineering |
| `dualsense_byte8_sweep.py`, `dualsense_targeted_sweep.py` | RE sweeps that mapped the `audio_control` byte |
| `dualsense_record_and_analyze.sh` | record + spectrogram helper |
