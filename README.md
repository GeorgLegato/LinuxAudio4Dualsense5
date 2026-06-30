# DualSense BT Speaker for Linux

**Use the PlayStation 5 DualSense controller's built-in speaker as a regular
audio output device over Bluetooth — on Linux, with no cable.**

## TL;DR — Steam Deck / SteamOS 🎮

Play game audio (and music) through your DualSense's **speaker or its headphone
jack — wirelessly**. No cable, no compiling.

1. **Download** the latest `ds5-bt-audio-*-x86_64.tar.gz` from the
   [**Releases**](https://github.com/GeorgLegato/LinuxAudio4Dualsense5/releases) page.
2. On the Deck, switch to **Desktop Mode**, open a terminal (Konsole), then:
   ```bash
   tar xzf ~/Downloads/ds5-bt-audio-*-x86_64.tar.gz
   cd ds5-bt-audio-*-x86_64
   ./install.sh
   ```
3. Pair your DualSense over **Bluetooth**, then pick **"DualSense BT Speaker"** as
   the audio output. Plug headphones into the controller? Toggle **Output → jack**
   in the web UI at **http://localhost:8118**. Done — it auto-starts from now on.

**After a SteamOS update?** → **Nothing to do.** It installs into your home
(`~/.local`), which SteamOS keeps across updates, so it just keeps working. Only
if a *future* SteamOS ever changes PipeWire so much that it breaks, grab the
newest release and run `./install.sh` again. Uninstall anytime: `./uninstall.sh`.

> Verified: the prebuilt binary loads cleanly against a current SteamOS image
> (glibc 2.41, PipeWire/Opus present). No root, no `pacman`, no dev packages.

---

Sony's official position is that the DualSense speaker only works over USB.
Over Bluetooth the controller speaks a proprietary, undocumented audio protocol
that no mainstream Linux tool implements. This project does: it registers a
native PipeWire sink called **"DualSense BT Speaker"** that you can select in
your normal sound menu, complete with a volume slider.

As of **0.2** it also drives the controller's two haptic actuators as a
**"Rumble as Subwoofer"** — a low-passed copy of the audio goes to the haptic
motors, so the bass you feel in your hands fills in the low end the little
membrane speaker can't reach. It's on by default and configurable, including a
small **built-in web UI with a live analyser** at `http://localhost:8118`.

**0.3** adds **Voice-In**: the DualSense's built-in microphone is exposed as a
recording device **"DualSense BT Mic"**, so any app (Audacity, calls, …) can
record from the controller over Bluetooth — full duplex alongside playback.
Off by default (it disturbs the gamepad input — see below).

```
PipeWire graph ──PCM──▶ ds5_membrane_sink ──Opus/0x36──▶ DualSense (Bluetooth)
                                   └─ low-pass ─▶ haptics (bass / "subwoofer")
```

## How it works

The DualSense speaker over Bluetooth is **not** raw PCM and **not** A2DP. It is
**Opus-encoded audio** wrapped in HID output report `0x36`. The key pieces that
make it work — none of them documented by Sony:

| Piece | Value |
|-------|-------|
| HID output report | `0x36` (398 bytes), four sub-packets: config `0x11`, state `0x10`, haptics `0x12`, **speaker `0x13`** |
| Audio codec | **Opus, 48 kHz stereo, CBR 160 kbps** → exactly 200 bytes/frame |
| Sink channels | **stereo: front-left = membrane speaker, front-right = haptic** (see "Two outputs" below). The membrane channel is duplicated to L/R for the Opus the controller expects. |
| Frame timing | **512 input samples → resampled to 480 → sent every 10.667 ms** (512/48000), *not* 10 ms |
| Checksum | CRC-32 with seed `0xA2` over the first 394 bytes |
| Speaker enable | report `0x31`: `audio_control = 0x30` (speaker path), preamp `0x02` |
| Config mask | `0x11` byte 4 = **`0xFE`** — bit 0 *cleared*. See "Audio + gamepad" below. |
| Haptics (subwoofer) | sub-packet `0x12`: **64 × int8 PCM @ 6 kHz** (one packet = one haptic frame). Low-passed bass, see "Rumble as Subwoofer". |
| Microphone (input) | comes back in the `0x31` input report (marker `0xD4`) as **Opus 48 kHz mono, 10 ms**. Enabled by config mask `0xFF`. See "Microphone / Voice-In". |

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

## Rumble as Subwoofer 🔊

The membrane speaker is tiny and has essentially no bass. But the DualSense has
**two voice-coil haptic actuators** that are, physically, little body-sound
subwoofers — a 50–200 Hz tone on the haptic route is *felt* in your hands and
*heard* as low-end pressure. So the sink sends a **low-passed copy of the audio
to the haptics** in parallel with the full-range signal to the speaker. On music
with a kick/bass drum this gives the controller a real low end. It is **on by
default**.

The haptic route is `0x12`: 64 × int8 PCM at 6 kHz, exactly one frame per
`0x36` packet. Per block: mix to mono → biquad low-pass → DC-block → decimate
48 kHz → 6 kHz (512 / 8 = 64 samples).

> Note: the actuators have a slow impulse response, so this adds *weight and
> pressure*, not a snappy transient — a sub, not a tweeter.

**Two outputs (stereo channel map).** The sink is presented to the OS as a
**stereo** device whose two channels are *not* left/right music but two physical
outputs:

- **front-left → the membrane speaker** (full range)
- **front-right → the haptic actuators** (run through the low-pass / gain / amp
  above; cutoff stays in the web UI)

This makes GNOME show **two testable speakers** ("Front Left" = membrane, "Front
Right" = haptic) and a balance slider. It also means **a normal stereo source
plays its left channel on the membrane and feeds its right channel to the
haptics** — for plain music listening, output mono (or know that the right
channel drives the rumble). Apps that want a dedicated rumble channel can send
bass straight to the right channel.

### Configure it — `~/.DS5/config`

On first run the service writes `~/.DS5/config` with the defaults. **Editing and
saving it takes effect within ~1 second — no restart needed** (the service
re-reads the file when its modification time changes). To turn the subwoofer off
entirely, set `subwoofer = off`:

```ini
output     = speaker  # speaker (internal membrane) | jack (3.5 mm headphone jack)
subwoofer  = on       # on | off
cutoff_hz  = 200      # low-pass cutoff in Hz (typ. 120..200)
gain       = 2.6      # haptic gain (higher = stronger / denser)
amp        = 64       # int8 amplitude cap, max 127
web        = on       # web UI / API (localhost only)
web_port   = 8118
microphone = off      # Voice-In recording device (see "Microphone" below)
leds       = off      # LED analyser: lightbar colour + player-LED VU (see below)
```

Since it's a systemd user service, a full restart also works if you prefer:
`systemctl --user restart ds5-membrane-sink`.

## Web UI & analyser — `http://localhost:8118`

The service has a small built-in web server (no extra dependency — plain C,
distribution-agnostic) so you can **tune by ear in the browser** and watch a
**live audio analyser showing the membrane / haptic split**:

- A spectrum where the **bass bands below the cutoff** (sent to the haptics) are
  drawn in amber and **everything above** (the membrane) in steel blue, with a
  red line marking the cutoff — drag the cutoff slider and watch the split move.
- Two level meters: **membrane** (full range) and **haptic** (the actual bass
  going to the actuators).
- Sliders for subwoofer on/off, cutoff, gain and amp cap. Changes apply
  **instantly** (over a WebSocket) and are saved to `~/.DS5/config`.

Open **http://localhost:8118** in any browser while audio plays.

- It binds **localhost only** — not reachable from the network, no auth needed.
- The analyser FFT only runs **while a browser is connected**, so the always-on
  service has zero overhead otherwise.
- The config file and the web UI are two views of the same settings; editing
  either updates the other.

> The web UI is the intended way to tweak. To find values purely on the command
> line instead, `reverse_engineered/sub_tune.py` is a curses tuner that sweeps
> cutoff / gain / amp live while music plays (it stops the service while running,
> then prints the command to start it again).

## LED analyser (Amiga-demo style) 🌈

The DualSense's LEDs can dance to the music. With `leds = analyser` (or the
web-UI toggle) the service turns the controller into a little spectrum display:

- **RGB lightbar** (the strips beside the touchpad) — colour follows the
  spectrum: **bass → blue, mids → red/warm, highs → white**, brightness = level.
- **5 white player LEDs** (below the touchpad) — a **VU bar** of the overall level.

It's a single-colour lightbar (one RGB zone, not a per-band rainbow strip), so
the colour reflects the *balance* of the sound rather than a per-frequency bar —
but it gives that old-demo vibe. LED control uses a dedicated `0x31` output
report (sent ~19×/s); turning it off hands the LEDs back to the system. Off by
default so it doesn't hijack the player indicator unless you want it.

> Reverse-engineered with `reverse_engineered/init_probe.py` — press `l` to take
> over the LEDs, then edit the `lightbar_R/G/B`, `playerLED` and `led_bright`
> fields live.

## Headphone jack output 🎧

The DualSense also has a **3.5 mm headphone jack**, and over Bluetooth the same
Opus stream can be routed there instead of the internal speaker — using sub-packet
`0x16` (headphone jack) in place of `0x13` (internal speaker). Set `output = jack`
(or flip the **"Ausgabe"** toggle in the web UI) to send audio to headphones
plugged into the controller; `output = speaker` (default) uses the membrane.

Unlike the mono membrane, the jack carries **true stereo**: in jack mode the
sink's left/right channels go to the left/right ear (in speaker mode the left
channel feeds the mono membrane). The haptic rumble keeps working in both modes.

> Found and dialed in with `reverse_engineered/init_probe.py` (press `j` to flip
> speaker/jack live, `a`/`d` to sweep the setup `audio_control` byte).

## Microphone / Voice-In 🎙️

The DualSense has a built-in microphone. Over Bluetooth its audio comes back
**Opus-encoded inside the HID input report** (48 kHz mono, 10 ms frames). With
`microphone = on` the service enables that duplex stream, decodes it, and
publishes a PipeWire **source "DualSense BT Mic"** — so it shows up as a normal
input device and any app (Audacity, a call, `parecord -d ds5_mic`) can record
from the controller. Playback and recording run **at the same time** (full
duplex). Toggle it in the web UI or via `microphone = on/off`.

> ⚠️ **It disturbs the gamepad input.** Sony's mic duplex sends the mic stream
> under the **same HID report id as gamepad input**, so the kernel/Steam misread
> it as stick deflection (the same mechanism we avoid for the speaker with mask
> `0xFE`). That's why it's **off by default** — turn it on only when you actually
> need the mic, and expect phantom stick input while it's on. (If you find a way
> to have both cleanly, PRs welcome.)

## Quick start

```bash
sudo apt install libpipewire-0.3-dev libopus-dev zlib1g-dev gcc make
cd src
make service
```

### Prebuilt install — SteamOS / Steam Deck (no compiler)

On immutable distros like **SteamOS** you can't easily install dev packages or
compile, so grab a **prebuilt release** instead — it installs entirely into your
`~/.local` (which survives OS updates), no root, no `make`, no pacman:

```bash
# download the latest ds5-bt-audio-*-x86_64.tar.gz from the Releases page, then:
tar xzf ds5-bt-audio-*-x86_64.tar.gz
cd ds5-bt-audio-*-x86_64
./install.sh          # -> ~/.local/bin + a systemd --user service
```

The release tarball is produced by `make release` (and by CI on every tag —
built on an older glibc so it runs on SteamOS and newer alike). It needs only
PipeWire + Opus at runtime, which SteamOS already has. Uninstall: `./uninstall.sh`.

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
speaker-enable. See [`usb/`](usb/). (On Linux **6.17+/6.18+** the kernel handles
the USB speaker/jack itself — see the section below — so this path is mostly
needed only on older kernels.)

```bash
pip install --user pydualsense
cd src && make service-usb
```

## Relation to mainline kernel support (USB vs Bluetooth)

In 2025 the kernel gained **DualSense audio support — but only over USB**:

- **Linux 6.17** — `ALSA: usb-audio`: jack detection for the controller's 3.5 mm
  port (knows when headphones are plugged in).
- **Linux 6.18** — `HID: playstation` (Cristian Ciocaltea / Collabora): reports
  headphone/headset-mic insert events via a dedicated input device, **routes
  audio to the internal mono speaker** (right channel → mono speaker) and bumps
  the speaker volume.

That work rides on the **USB audio-class interface**, which the DualSense only
exposes **over a cable**.

**Nothing here requires kernel 6.17/6.18 — both of this project's variants run
on older kernels too:**

- **Bluetooth (the core product):** the controller exposes **no USB audio
  interface**. Its speaker, microphone and haptics live in a proprietary
  **Opus-in-HID** stream (report `0x36`, mic marker `0xd4`) that **no kernel
  driver implements — on any version**. This is exactly what the project
  reverse-engineers; it needs only PipeWire + `/dev/hidraw` and is independent
  of the kernel's audio support. The mainline patches change **nothing** for the
  wireless case.
- **USB:** over a cable the DualSense has *always* been a generic USB-audio card
  (long before 6.17). What 6.17/6.18 add is the **automation** — jack detection,
  routing to the mono speaker, volume. Our `make service-usb` does that same work
  by hand (HID speaker-enable + PipeWire routing), so it **also needs no special
  kernel** — it just becomes **redundant** once the kernel does it for you.

So who is this for:

| | Linux **< 6.17** | Linux **≥ 6.17/6.18** |
|---|---|---|
| **Bluetooth** | this project ✅ (only option) | this project ✅ (kernel still can't do BT) |
| **USB / cable** | this project's USB variant ✅ (kernel can't yet) | use the kernel — USB variant redundant |

In short: the **wireless** scenario — the whole point of this project — is its
lasting contribution on every kernel; the **cabled** scenario is moving into the
kernel from 6.17 on, but our USB variant still serves anyone on an older one.
(Nice cross-check: the kernel's "right channel → mono speaker" mirrors our
front-left/front-right channel split.)

References: [Phoronix — 6.18 jack handling](https://www.phoronix.com/news/Sony-DualSense-Audio-Handling),
[LWN](https://lwn.net/Articles/1026850/),
[ALSA usb-audio patch](https://patchew.org/linux/20250526-dualsense-alsa-jack-v1-0-1a821463b632@collabora.com/).

## Repository layout

```
src/                    native PipeWire sink over BLUETOOTH (the real product)
  ds5_membrane_sink.c   C: PipeWire node + Opus + 0x36 + CRC + haptic subwoofer + web UI
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
  sub_tune.py           live tuner for the "Rumble as Subwoofer" cutoff/gain
  init_probe.py         TUI that found the mask bit-0 mic-capture switch
  check_input.py        shows gamepad vs d4-feedback rate from hidraw
  run.sh                test harness (tones, sweeps, diagnostics)
  membrane-sink.sh      null-sink + parec workaround (pre-native-node)
```

## Status

- ✅ Membrane speaker over Bluetooth, smooth, full range (verified to 2 kHz+)
- ✅ Native PipeWire sink, selectable device, volume control
- ✅ Microphone muted, indicator LED quiet, no input-event interference
- ✅ Audio + gamepad work simultaneously (mask `0xFE`)
- ✅ "Rumble as Subwoofer": low-passed bass on the haptics, live-configurable
- ✅ Built-in web UI + live analyser at `localhost:8118` (no extra dependency)
- ✅ Voice-In: microphone as a recording device "DualSense BT Mic" (opt-in, full duplex)
- ✅ Output switchable to the 3.5 mm headphone jack (`output = jack`, true stereo)
- ✅ LED analyser: lightbar colour + player-LED VU follow the music (`leds = analyser`)
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
