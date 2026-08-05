# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Build the CueForge executable with PyInstaller (onefile).

Bundles into a single self-extracting binary:
- the web UI (cueforge/web) so the server can serve it
- sounddevice (PortAudio), soundfile (libsndfile), soxr (resampler),
  uvicorn, qrcode, PIL data

The ffmpeg and yt-dlp binaries are NOT bundled. They ship as an external
``vendored/`` folder next to the executable (see copy_vendored); the runtime
resolvers look for ``<exe dir>/vendored/<tool>/<tool>`` when frozen.

Run:    python build_exe.py
Output: ``dist/CueForge.exe`` on Windows, ``dist/CueForge-linux-<arch>`` on
Linux -- named to match the release asset the in-app updater looks for -- plus
``dist/vendored/`` when the vendored binaries are present in the repo.

Linux note: sounddevice publishes no wheel carrying a Linux PortAudio, so the
system ``libportaudio.so.2`` is located and bundled here (with a runtime hook to
make sounddevice actually look at it). Install ``libportaudio2`` before building.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys

import PyInstaller.__main__

from cueforge.platform_util import IS_LINUX, IS_WINDOWS, EXE_SUFFIX, release_asset_name

SEP = ";" if IS_WINDOWS else ":"
HERE = os.path.dirname(os.path.abspath(__file__))

#: The artifact name. PyInstaller appends ``.exe`` itself on Windows, so the
#: name it is given is the asset name minus that suffix.
ASSET_NAME = release_asset_name()
PYI_NAME = ASSET_NAME[: -len(EXE_SUFFIX)] if EXE_SUFFIX else ASSET_NAME

# (source in repo, destination subfolder under dist/vendored/)
VENDORED = [
    (f"vendor/ffmpeg/ffmpeg{EXE_SUFFIX}", "ffmpeg"),
    (f"vendor/yt-dlp/yt-dlp{EXE_SUFFIX}", "yt-dlp"),
]


def copy_vendored(dist_dir: str) -> None:
    """Copy the vendored ffmpeg/yt-dlp binaries into ``<dist>/vendored/``.

    Each binary is copied only when it exists in the repo; a missing source
    is skipped (not an error) so the build still produces a lone executable.
    """
    for src, subdir in VENDORED:
        if os.path.isfile(src):
            dest_dir = os.path.join(dist_dir, "vendored", subdir)
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(dest_dir, os.path.basename(src)))
            print(f"[OK] vendored {src} -> {dest_dir}")
        else:
            print(f"[skip] {src} not found; skipping (downloaded on first use)")


def find_portaudio() -> str:
    """Absolute path to the system ``libportaudio.so.2`` to bundle (Linux only).

    ``ctypes.util.find_library`` only hands back a soname, so resolve it to a
    real file through ``ldconfig -p`` and fall back to scanning the usual
    library directories (which also covers a musl system, where there is no
    ``ldconfig -p``). Raises when nothing is found -- a release binary that
    cannot open an audio device is not worth publishing.
    """
    from ctypes.util import find_library

    soname = find_library("portaudio") or "libportaudio.so.2"

    try:
        out = subprocess.run(
            ["ldconfig", "-p"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=15,
        ).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        if soname in line and "=>" in line:
            path = line.split("=>")[-1].strip()
            if os.path.isfile(path):
                return os.path.realpath(path)

    for lib_dir in ("/usr/lib64", "/lib64", "/usr/lib", "/lib",
                    "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu"):
        for cand in sorted(glob.glob(os.path.join(lib_dir, "libportaudio.so*"))):
            if os.path.isfile(cand):
                return os.path.realpath(cand)

    raise SystemExit(
        "[error] libportaudio not found -- CueForge cannot play audio without it.\n"
        "        Install it and build again, e.g.\n"
        "          Debian/Ubuntu: sudo apt install libportaudio2\n"
        "          Fedora:        sudo dnf install portaudio\n"
        "          Arch:          sudo pacman -S portaudio"
    )


def main() -> None:
    args = [
        "cueforge/__main__.py",
        "--name", PYI_NAME,
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        # data / binaries (ffmpeg & yt-dlp ship externally, see copy_vendored)
        f"--add-data=cueforge/web{SEP}cueforge/web",
        # third-party packages that load data/submodules dynamically
        "--collect-all", "sounddevice",
        "--collect-all", "soundfile",
        "--collect-all", "soxr",
        "--collect-all", "uvicorn",
        "--collect-all", "qrcode",
        "--collect-submodules", "uvicorn",
        # make sure the app package itself is fully collected
        "--collect-submodules", "cueforge",
        # commonly-needed hidden imports
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
    ]

    if IS_WINDOWS:
        # An .ico is a PE resource; PyInstaller has nothing to do with one on ELF.
        args += ["--icon", "assets/CueForge.ico"]

    if IS_LINUX:
        portaudio = find_portaudio()
        print(f"[OK] bundling PortAudio from {portaudio}")
        args += [
            f"--add-binary={portaudio}{SEP}.",
            "--runtime-hook", os.path.join(HERE, "scripts", "pyi_rth_portaudio.py"),
        ]

    PyInstaller.__main__.run(args)
    copy_vendored("dist")

    built = os.path.join("dist", ASSET_NAME)
    if not os.path.isfile(built):
        raise SystemExit(f"[error] expected {built} to exist after the build")
    print(f"[OK] built {built} ({os.path.getsize(built) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
