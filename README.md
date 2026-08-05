<p align="center">
  <img src="assets/Logo.png" alt="" width="160">
</p>

<h1 align="center">CueForge</h1>

<p align="center"><em>Audio cues for the small stage.</em></p>

<p align="center">
  <a href="https://lesani.github.io/CueForge/"><img src="https://img.shields.io/badge/website-lesani.github.io%2FCueForge-2ea44f" alt="Website"></a>
  <a href="https://github.com/Lesani/CueForge/releases/latest"><img src="https://img.shields.io/github/v/release/Lesani/CueForge?label=download" alt="Latest release"></a>
  <a href="https://github.com/Lesani/CueForge/releases/latest"><img src="https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-informational" alt="Platforms: Windows and Linux"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License: AGPL-3.0"></a>
  <a href="https://ko-fi.com/lesani"><img src="https://img.shields.io/badge/Ko--fi-support%20development-FF5E5B?logo=ko-fi&logoColor=white" alt="Ko-fi"></a>
</p>

<p align="center">
  <a href="https://lesani.github.io/CueForge/"><b>Website</b></a> ·
  <a href="#download"><b>Download</b></a> ·
  <a href="#quick-start-from-source"><b>Run from source</b></a> ·
  <a href="CHANGELOG.md"><b>Changelog</b></a> ·
  <a href="CONTRIBUTING.md"><b>Contributing</b></a>
</p>

A reliable, server-side **audio cue system for amateur and small-scale
productions** — amateur theatre, community stages, dance, school and church
events, small venues — driven from a browser. Run the server on the booth
machine, then control the show from any browser on the network — laptop,
tablet, or phone. Multiple clients share one live show state.

The machine running the server is the one that produces the audio (it feeds the
sound board), so cue playback is centralized and deterministic; the browsers are
just controllers.

## Built for the small stage

CueForge deliberately does **less** than professional show-control suites.
Amateur productions run on limited time, borrowed hardware, and crews that
change from show to show — so the tool should be learnable in an afternoon
and dependable on show night:

- **Fire cues, in order, reliably.** A grid of buttons and a big GO. No
  scripting, no MIDI routing matrices, no session concepts.
- **Plain language everywhere.** Volume instead of gain staging, "Plays as
  Background" instead of bus assignments, fades you drag directly on the
  waveform.
- **One file per show.** Carry the whole production on a USB stick; open it on
  another machine and it plays.

If you need SMPTE timecode, MIDI control, or many-bus routing, have a look at
[LivePlay](https://tdoukinitsas.github.io/liveplay/) — a fellow open-source
(AGPL) cue system with a professional feature set. If you need tonight's show
to just work from a laptop and a pair of speakers (or a handful of them),
CueForge is for you.

![CueForge playing view — cue running with progress, looping background bed, stop cues](assets/screenshot-playing-live.png)

## Download

Grab the prebuilt binary for your machine from the
[latest release](https://github.com/Lesani/CueForge/releases/latest) — no
install needed, just run it. It prints a URL, PIN, and a scannable QR code, and
opens the control UI in your browser.

| Platform | File |
|---|---|
| Windows | `CueForge.exe` |
| Linux (Intel/AMD 64-bit) | `CueForge-linux-x86_64` |
| Linux (ARM 64-bit, e.g. Raspberry Pi 4/5) | `CueForge-linux-aarch64` |

On Linux, mark it executable first — release downloads never carry that bit:

```sh
chmod +x CueForge-linux-x86_64
./CueForge-linux-x86_64
```

The Linux builds bundle everything they need except the C library and ALSA,
both present on any normal desktop or server install. They are built against
glibc 2.35, so they run on Ubuntu 22.04+, Debian 12+, Fedora 36+ and anything
newer. On an older distro, run from source instead.

CueForge keeps itself current: it checks GitHub for new releases (Settings →
Application, can be turned off) and updates itself with one click — download,
swap, restart.

Running from source instead? See [Quick start](#quick-start-from-source) below.

## Features

- **Playing view** — a page/column grid of cues. `GO` advances column-major
  through the current page; tap any cue to fire it directly. A moving cursor
  marks the next cue, played cues turn green, and a large fixed-width timer
  anchors the bottom bar.
- **Cue model** — exclusive *normal* cues (a new one hard-cuts the previous with
  a declick), stackable looping *background* layers with fade-in, and *stop*
  cues (targeted or all, hard or timed fade). Every cue wears its type in the
  grid — violet stripe + `bg` tag for backgrounds, red stripe for stops — so a
  glance tells you what `GO` will do. A one-tap **PANIC** fast-fades everything.
- **Library** — import audio in any format (decoded to 48 kHz via ffmpeg,
  fetched automatically on first use) or straight from YouTube via yt-dlp.
  Content-hash de-duplication, collapsible groups, filter and sort, per-item
  trim/gain/fades on a zoomable waveform editor, audition preview (server
  output or "this device"), and WAV/FLAC/MP3 export with cue params baked in.
- **Authoring** — edit mode to build pages/columns, place and rearrange cues by
  drag-and-drop (touch included), and drop audio files straight onto grid cells.
- **Portable projects** — a show is a single `.cueforge` file (zip of
  `show.json` + decoded audio); an empty project opens on startup and autosaves.
  **Save to device / Load from device** in Settings downloads or uploads the
  whole show through the browser, so you can carry it to another machine on a
  USB stick (uploads never overwrite — a name clash lands as `Name (2)`).
- **Easy access** — the console prints a URL, PIN, and scannable QR; the
  in-app Settings tab shows the same for tablets. Remote clients need the PIN;
  loopback is trusted. On iPad/iPhone, *Add to Home Screen* gives a true
  fullscreen control surface, and a screen wake lock keeps devices awake
  mid-show.

| Library — waveform editor, groups | Settings — access, output, updates |
|---|---|
| ![Library](assets/screenshot-library.png) | ![Settings](assets/screenshot-settings.png) |

## Tech stack

- **Backend:** Python 3.13, FastAPI + uvicorn, WebSockets for live state.
- **Audio:** a NumPy real-time mixer over `sounddevice` (PortAudio); `soundfile`
  (libsndfile/FLAC); `ffmpeg` for decoding on import.
- **Frontend:** vanilla JavaScript ES modules + canvas — **no build step**.
- **Packaging:** PyInstaller (onefile) into a single binary per platform.
- **Platforms:** Windows and Linux (x86_64 and aarch64).

## Quick start (from source)

Requires Python 3.13. `ffmpeg` and `yt-dlp` are **not** needed up front —
CueForge downloads its own copies on first use.

On Linux, install PortAudio first — `sounddevice` publishes no wheel that
carries it, so it has to come from your package manager:

```sh
sudo apt install libportaudio2     # Debian/Ubuntu
sudo dnf install portaudio         # Fedora
sudo pacman -S portaudio           # Arch
```

Then, on either platform:

```sh
python -m venv .venv

# Windows
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m cueforge

# Linux
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m cueforge
```

Then open the printed URL (default http://localhost:7070/). Press **F11** for
fullscreen. Set the output device in **Settings → Output** before playing.

Want something to click on right away? Generate the demo show from the
screenshots (synthesized audio, no downloads):

```sh
.venv/Scripts/python.exe scripts/make_demo_show.py   # Windows
.venv/bin/python scripts/make_demo_show.py           # Linux
```

and open the **Demo** project from Settings → Saved projects.

Environment overrides:

- `CUEFORGE_PORT` — listen port (default `7070`)
- `CUEFORGE_NO_BROWSER=1` — don't auto-open a browser (useful on a headless
  booth machine, where there is no browser to open)
- `CUEFORGE_FFMPEG` — path to an ffmpeg binary, instead of the downloaded one
- `CUEFORGE_YTDLP` — path to a yt-dlp binary, instead of the downloaded one

## Data location

Per-user data lives under `~/CueForge` (`C:\Users\you\CueForge` on Windows,
`/home/you/CueForge` on Linux):

- `config.json` — output device, master trim, PIN, port, update preference
- `projects/*.cueforge` — saved shows
- `work/` — extracted working folders for open projects
- `bin/` — auto-downloaded ffmpeg / yt-dlp

A show is fully portable between platforms: the `.cueforge` file holds
`show.json` plus decoded audio, so a show built on Windows opens on Linux and
back. Only the **Outputs** are venue-specific — re-point them at the local
devices in **Settings → Audio outputs** on the new machine.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev
environment, project layout, tests, building the binary, and how to open a
pull request. The WebSocket/REST protocol is documented in
[`cueforge/server/PROTOCOL.md`](cueforge/server/PROTOCOL.md), and the
architectural decisions behind the engine, cue model and packaging are recorded
as [ADRs](docs/adr/). Release history lives in [CHANGELOG.md](CHANGELOG.md).

When proposing features, keep the project's focus in mind: CueForge stays small
and approachable — features that mainly serve large, complex productions are
usually a better fit for other tools.

If CueForge is useful to you and you'd like to support its development, you can
[buy me a coffee on Ko-fi](https://ko-fi.com/lesani). ♥

## Third-party components

CueForge depends on the Python packages listed in `requirements.txt` (FastAPI,
uvicorn, NumPy, sounddevice/PortAudio, soundfile/libsndfile, soxr, qrcode,
Pillow, and others — each under its own license). In addition, two external
tools are **downloaded on first use** rather than bundled with this repository:

- **[ffmpeg](https://ffmpeg.org/)** — used to decode imported audio and to
  encode exports. The auto-downloaded build comes from
  [gyan.dev](https://www.gyan.dev/ffmpeg/) on Windows and from
  [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) on Linux (the
  `gpl` static build, which is the variant that carries the `libmp3lame`
  encoder the MP3 export needs). Licensed under the GPL/LGPL. Not distributed
  as part of this project.
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — used for the optional YouTube
  import feature. Released into the public domain (Unlicense). Not distributed as
  part of this project.

Please respect the terms of service of any site you download from.

## License

CueForge is licensed under the **GNU Affero General Public License v3.0 or
later** (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

Because CueForge is a network-server application, the AGPL requires that if you
run a modified version and let others interact with it over a network, you must
also offer them the corresponding source code of your modified version.

Copyright (C) 2026 Lesani.
