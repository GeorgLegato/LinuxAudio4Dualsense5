# DualSense-only games 🕹️

Tiny games that run **entirely on the DualSense over Bluetooth** — the only
"screen" is the controller's LEDs, the only feedback is sound (membrane speaker),
rumble and the adaptive triggers. Each script stops the audio service while it
runs (it needs to be the sole sender) and restarts it on exit.

| Game | What it is | Controls |
|------|-----------|----------|
| `ds5_15d_shooter.py` | A "1.5D" shooter on the 5 player LEDs — enemies march in, shoot the nearest. | left stick / X (cross) to fire |
| `ds5_maze.py` | Walk a maze felt through the LEDs (lightbar = wall ahead, player LEDs = walls left/right) with step/oompf sound. **Triangle = echolocation ping** (2D-acoustics echoes); 3D debug view at `localhost:8119`. | left stick (turn / step), Triangle |
| `ds5_safecracker.py` | Haptic lock-picking: hold R2 tension, raise pins by feel — rumble swells near the shear line, click to set, don't overset before the alarm goes red. | R2 (tension), left stick up (raise pin) |

```bash
cd games
./ds5_safecracker.py
```

They read input straight from `/dev/hidraw` and drive the LEDs / triggers / rumble
via the `0x31` output report and sound via Opus `0x36` — the same primitives the
main project reverse-engineered. Keyboard fallbacks are included where useful.
