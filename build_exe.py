# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Build CueForge.exe with PyInstaller (Windows, onefile).

Bundles into a single self-extracting CueForge.exe:
- the web UI (cueforge/web) so the server can serve it
- sounddevice (PortAudio), soundfile (libsndfile), soxr (resampler),
  uvicorn, qrcode, PIL data

The ffmpeg and yt-dlp binaries are NOT bundled. They ship as an external
``vendored/`` folder next to the exe (see copy_vendored); the runtime
resolvers look for ``<exe dir>/vendored/<tool>/<exe>`` when frozen.

Run: .venv/Scripts/python.exe build_exe.py
Output: dist/CueForge.exe (a console app), plus dist/vendored/ when the
vendored binaries are present in the repo.
"""

from __future__ import annotations

import os
import shutil

import PyInstaller.__main__

SEP = ";" if os.name == "nt" else ":"

# (source in repo, destination subfolder under dist/vendored/)
VENDORED = [
    ("vendor/ffmpeg/ffmpeg.exe", "ffmpeg"),
    ("vendor/yt-dlp/yt-dlp.exe", "yt-dlp"),
]


def copy_vendored(dist_dir: str) -> None:
    """Copy the vendored ffmpeg/yt-dlp binaries into ``<dist>/vendored/``.

    Each binary is copied only when it exists in the repo; a missing source
    is skipped (not an error) so the build still produces a lone exe.
    """
    for src, subdir in VENDORED:
        if os.path.isfile(src):
            dest_dir = os.path.join(dist_dir, "vendored", subdir)
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(src, os.path.join(dest_dir, os.path.basename(src)))
            print(f"[OK] vendored {src} -> {dest_dir}")
        else:
            print(f"[skip] {src} not found; skipping (exe will rely on PATH)")


def main() -> None:
    args = [
        "cueforge/__main__.py",
        "--name", "CueForge",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--icon", "assets/CueForge.ico",
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
    PyInstaller.__main__.run(args)
    copy_vendored("dist")


if __name__ == "__main__":
    main()
