# 7. ffmpeg is always a build we fetched ourselves, sourced per platform

Date: 2026-08-05

## Status

Accepted

## Context

CueForge has never fallen back to an ffmpeg on `PATH`: every import decodes
through ffmpeg, and MP3 export encodes through `libmp3lame`, so an arbitrary
build with different codecs is a show-night failure waiting to happen. On
Windows that rule costs nothing — there is no system ffmpeg to be tempted by,
and gyan.dev publishes a `release-essentials` zip with a documented `.ver` and
`.sha256` beside it.

Adding Linux puts the rule under real pressure for the first time. Every distro
ships an ffmpeg package, `apt install ffmpeg` is one line, and the honest cost
of refusing it is a ~125 MB download on first run. A future reader looking at
`ffmpeg_util.py` on a machine that already has `/usr/bin/ffmpeg` will reasonably
ask why we ignore it.

Options considered:

1. **Download a controlled static build**, as on Windows. Keeps one known-good
   binary everywhere; costs the download and needs a Linux publisher.
2. **Prefer the system ffmpeg, download only as a fallback.** Cheap and
   idiomatic for Linux, but silently makes export behaviour depend on how the
   user's distro compiled ffmpeg.
3. **Require the system ffmpeg** and error out with an install hint. No download
   code at all, but audio import becomes a documentation problem.

Distro builds are the crux: Debian and Fedora both split ffmpeg such that
`libmp3lame` is not guaranteed present, so options 2 and 3 make MP3 export work
on the maintainer's machine and fail on a user's.

## Decision

Option 1. Linux fetches the **`gpl` static tarball from BtbN/FFmpeg-Builds**
(x86_64 and aarch64), verified against the release's `checksums.sha256`;
Windows keeps gyan.dev unchanged. `gpl` rather than `lgpl` specifically because
the lgpl variant omits `libmp3lame`.

The resolution order is unchanged and still has no `PATH` step:
`CUEFORGE_FFMPEG` → download cache → `vendored/` beside the binary →
`vendor/` in the repo.

## Consequences

- First run on Linux downloads ~125 MB before audio import works. The launcher
  already reports download progress, so this degrades visibly rather than
  silently.
- `CUEFORGE_FFMPEG` is the supported escape hatch for anyone who genuinely
  wants their own build (an air-gapped booth, an unsupported architecture).
  It is checked first precisely so this policy is never a dead end.
- BtbN publishes no per-package version endpoint, so "latest" on Linux is the
  newest release *branch* (`n8.1` → `"8.1"`) while the installed build reports a
  patch (`"8.1.2"`). Those are compared numerically, not for equality — an
  equality check reads a patch ahead of its branch as a permanent available
  update. Any future source must keep that comparison numeric.
- A checksum listing that is reachable but does not name our asset is treated as
  an error, not as "skip verification". An unreachable checksum host is still
  tolerated, so a first run survives the publisher being briefly down.
- Adding a platform means adding a publisher, not just an asset name. There is
  no generic "download ffmpeg" path to inherit.
