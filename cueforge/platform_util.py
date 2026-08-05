# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Platform and architecture identity, in one place.

Four modules need to agree on "which build of CueForge is this, and which
third-party binaries match it": the ffmpeg and yt-dlp resolvers pick a download
asset, the self-updater picks a release asset, and ``build_exe.py`` names the
artifact it produces. Deriving that independently in each is how a Windows
assumption creeps back in, so it lives here.

Deliberately stdlib-only and side-effect free -- the low-level ``*_util``
modules import it, and so does ``build_exe.py`` from outside the package.

Supported targets: Windows x86_64, Linux x86_64, Linux aarch64.
"""

from __future__ import annotations

import os
import platform
import sys

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

#: Suffix for executables on this platform ("" everywhere but Windows).
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""

# platform.machine() reports the same CPU under several names depending on the
# OS and the interpreter's own bitness (AMD64 on Windows, x86_64 on Linux;
# arm64 on macOS, aarch64 on Linux). Normalise to the Linux spellings, which
# are what the upstream projects we download from use in their asset names.
_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}


def arch_tag(machine: str | None = None) -> str:
    """Normalised CPU architecture, e.g. ``"x86_64"`` or ``"aarch64"``.

    ``machine`` defaults to :func:`platform.machine`; pass one explicitly to
    test the mapping. An unrecognised value is passed through lowercased rather
    than guessed at, so a future target surfaces as an obvious "no such asset"
    instead of silently downloading the wrong binary.
    """
    raw = (machine if machine is not None else platform.machine()).strip().lower()
    return _ARCH_ALIASES.get(raw, raw)


def platform_tag() -> str:
    """``"<os>-<arch>"`` for this machine, e.g. ``"linux-aarch64"``."""
    system = "windows" if IS_WINDOWS else ("linux" if IS_LINUX else sys.platform)
    return f"{system}-{arch_tag()}"


def release_asset_name(tag: str | None = None) -> str:
    """Name of the CueForge release asset for a platform.

    Windows keeps the bare ``CueForge.exe`` it has always published (the
    in-app updater on already-released builds looks for exactly that name, so
    renaming it would strand them). Every other target is suffixed:
    ``CueForge-linux-x86_64``, ``CueForge-linux-aarch64``.

    ``tag`` defaults to this machine's :func:`platform_tag`.
    """
    tag = tag or platform_tag()
    if tag.startswith("windows-"):
        return "CueForge.exe"
    return f"CueForge-{tag}"


def make_executable(path: str) -> None:
    """Set the owner/group/other execute bits on ``path`` (no-op on Windows).

    Binaries arrive from GitHub release assets and tar/zip extraction without
    a usable mode, so everything we download and then exec goes through here.
    """
    if IS_WINDOWS:
        return
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | 0o111)
    except OSError:
        pass  # best-effort; a genuinely unusable binary fails loudly at exec
