# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [2026-07-03]

### Added
- Prebuilt Windows binaries: pushing a `v*` tag builds `CueForge.exe` on CI and publishes it (with `SHA256SUMS.txt`) as a GitHub Release.
- In-app self-update: Settings shows an update badge when a newer release exists; one click downloads, checksum-verifies, swaps the exe, and restarts. "Check for updates" setting (default on).
- Demo show generator (`scripts/make_demo_show.py`): a two-act show with synthesized audio for trying CueForge without importing anything.
- `.cueforge` files now carry a `formatVersion` so future format changes can migrate; files from a newer format are refused with a clear error.
- Ko-fi support link in the README and Settings.

### Changed
- README reworked for the first public release, with fresh screenshots from the demo show; dev/maintainer docs (tests, exe build, CI, layout) moved to CONTRIBUTING.
- SPDX license headers on all Python sources; `pyproject.toml` declares runtime dependencies.

### Fixed
- Self-update restart: the old process hung waiting for open WebSockets, and the spawned replacement inherited the PyInstaller bootloader environment and died silently; both fixed, shutdown is clean (no traceback/leak spam) and the new version starts reliably.
- Console dashboard now shows the running app version.
- Background and stop cues are now visually distinct before playback (violet stripe + `bg` tag, red stripe for stops).
- Security hardening: project names and audio hashes are validated before any filesystem path is built (path-traversal), `.cueforge` extraction is capped and filtered (zip-bomb), the updater refuses unverified downloads, PIN comparison is constant-time, FLAC imports write atomically, and the mixer silences NaN/inf samples.

## [2026-07-02]

### Added
- Save to device / Load from device: export or import a whole project (`.cueforge`) through the browser to carry a show between machines (USB); uploads auto-rename on name collision and never overwrite an existing show.
- AGPL-3.0 license and a CONTRIBUTING guide; repository prepared for public open-source release.
- PWA support: Add to Home Screen gives a true fullscreen app on iPad/iPhone (manifest, icons, apple meta).
- Cue export: download any cue as WAV/FLAC/MP3 with trim/gain/fades baked in.
- Stop cues can be created directly ("Stop cue" button, "+ New stop cue here" in the picker) without importing audio.
- Library groups: assign items to a group (editable dropdown), collapsible group headers, group sort; picker groups too.
- Library list: filter box and sort (name/type/duration/group).
- Touch drag & drop for rearranging cues in edit mode (iPad/phone).
- Waveform touch gestures: pinch zoom and one-finger pan when zoomed.
- Screen wake lock keeps control devices awake mid-show.
- Server console: live stats line (uptime/clients/voices/CPU/RAM) and window title; app logo in top bar and as exe/web icon.
- ffmpeg is auto-downloaded from gyan.dev into `~/CueForge/bin` on startup when none is bundled, with live download progress in the server console and a non-blocking toast in the web UI.
- yt-dlp is auto-downloaded from GitHub on the first YouTube import when none is available (previously it just errored).
- ffmpeg update prompt: when a newer release exists, the web UI offers Update now (with progress) / Dismiss / Don't show again for this version.

### Changed
- Full responsive layout for phones/tablets: two-row control bar with large GO, scroll-snapping cue columns, stacked library panes, touch-sized targets.
- Top bar tabs collapse into a hamburger dropdown below 600px so Playing/Library/Settings never push off-screen on phones.
- Server console redesigned as a two-column dashboard: ASCII-art logo with a live stats table (uptime/clients/voices/CPU/RAM) on the left, right-aligned join QR with Local/Network/PIN on the right, redrawn in place every 2 s.
- Redesigned "place a cue" picker: large searchable sheet with type badges, durations, and group headers.
- Native confirm/prompt/alert dialogs replaced with themed in-app dialogs (required for iOS home-screen mode).
- Build now produces a single `CueForge.exe` (onefile); ffmpeg/yt-dlp ship as an external `vendored/` folder beside it when present.
- ffmpeg is only ever a copy CueForge controls (download cache / bundled / repo) — no fallback to a system or imageio ffmpeg, which could be arbitrary builds.

### Removed
- `imageio-ffmpeg` dependency (no longer a resolver fallback).

### Fixed
- Orphaned partial download files (`*.part`) from an interrupted ffmpeg/yt-dlp fetch are now removed automatically so `~/CueForge/bin` never accumulates junk.
- Remote devices (iPhone/iPad) failed all REST calls without the PIN: waveforms, settings, import, and export now authenticate correctly.
- Waveform display on iOS (FLAC decode unsupported there; server now provides a WAV fallback).
- iPad Safari toolbar no longer shifts the app content (dynamic viewport units + safe-area insets).
- Choppy playback animation (stutter-free timer/fill) and choppy waveform playhead (layered canvas rendering).
- Background cue running times were frozen in the grid; they now tick.
- Faster reconnect after a device returns from background; PIN remembered across reloads.
- Double-tap zoom on rapid GO taps and pull-to-refresh mid-show are disabled.
- ffmpeg startup download no longer froze the web UI/server (the resolver blocked the async event loop for the whole download).

## [2026-07-01]

### Added
- Reopen the last opened project on startup (tracked in `~/CueForge/config.json`), falling back to a fresh Untitled canvas.

### Changed
- Moved the edit-mode "Add column" action to the toolbar so entering edit mode no longer shifts the grid layout.
