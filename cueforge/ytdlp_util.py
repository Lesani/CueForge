# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Locate -- and, when missing, download -- the yt-dlp binary used for YouTube
audio import.

Resolution order (first hit wins):
  1. CUEFORGE_YTDLP env var (explicit override)
  2. Downloaded cache (~/CueForge/bin/yt-dlp[.exe])
  3. Frozen build: vendored binary beside the exe
     (<exe dir>/vendored/yt-dlp/yt-dlp[.exe])
  4. Vendored binary (repo_root/vendor/yt-dlp/yt-dlp[.exe])
  5. yt-dlp on PATH

Unlike ffmpeg, yt-dlp is a single self-contained executable, so provisioning is
a trivial one-file download from the GitHub "latest" release (and yt-dlp then
self-updates via ``yt-dlp -U``). :func:`ensure_ytdlp` resolves an existing copy
or downloads the latest into the cache dir on first use.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import urllib.request
from functools import lru_cache

from cueforge.platform_util import EXE_SUFFIX, IS_WINDOWS, arch_tag, make_executable

_EXE = "yt-dlp" + EXE_SUFFIX

# Downloaded binaries share the server's writable home dir (mirrors
# cueforge.ffmpeg_util.CACHE_DIR) -- kept local so this module stays standalone.
_HOME_DIR = os.path.join(os.path.expanduser("~"), "CueForge")
CACHE_DIR = os.path.join(_HOME_DIR, "bin")

# GitHub serves a permanent "latest" redirect to each single-file build. Pick
# the standalone binary for this platform: the bare ``yt-dlp`` asset is the
# zipapp, which needs a system Python 3 -- fine from source, but not something
# the frozen build can assume is installed on a booth machine.
_ASSETS = {
    "windows-x86_64": "yt-dlp.exe",
    "windows-aarch64": "yt-dlp_arm64.exe",
    "linux-x86_64": "yt-dlp_linux",
    "linux-aarch64": "yt-dlp_linux_aarch64",
}


def _asset_name() -> str:
    """Release asset matching this platform; the zipapp as a last resort."""
    system = "windows" if IS_WINDOWS else "linux"
    return _ASSETS.get(f"{system}-{arch_tag()}", "yt-dlp")


def download_url() -> str:
    """Permanent "latest release" URL for this platform's yt-dlp build."""
    return f"https://github.com/yt-dlp/yt-dlp/releases/latest/download/{_asset_name()}"

_HTTP_TIMEOUT = 30  # per-read socket timeout (seconds)
_CHUNK = 1024 * 256

# Serializes concurrent downloads (two YouTube imports racing when yt-dlp is
# missing) so they never write the same ``.part`` at once. Unlike ffmpeg, the
# yt-dlp download has no provision-lock upstream, so it guards itself here.
_download_lock = threading.Lock()


@lru_cache(maxsize=1)
def resolve_ytdlp() -> str:
    """Return an absolute path to a usable yt-dlp binary, or raise."""
    env = os.environ.get("CUEFORGE_YTDLP")
    if env and os.path.isfile(env):
        return env

    cached = os.path.join(CACHE_DIR, _EXE)
    if os.path.isfile(cached):
        return cached

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cand = os.path.join(exe_dir, "vendored", "yt-dlp", _EXE)
        if os.path.isfile(cand):
            return cand

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cand = os.path.join(repo_root, "vendor", "yt-dlp", _EXE)
    if os.path.isfile(cand):
        return cand

    on_path = shutil.which("yt-dlp")
    if on_path:
        return on_path

    raise FileNotFoundError(
        "yt-dlp not found. Set CUEFORGE_YTDLP, place a binary in "
        "vendor/yt-dlp/, or let CueForge download one on first use."
    )


def cleanup_partials() -> None:
    """Delete a leftover ``yt-dlp[.exe].part`` from an interrupted download.

    A clean download removes its ``.part`` on the error path; a *hard* kill can
    orphan one. Called at startup so the cache dir never accumulates junk. Only
    touches yt-dlp's own temp name, so it is safe to run alongside an unrelated
    (e.g. ffmpeg) download.
    """
    try:
        os.remove(os.path.join(CACHE_DIR, _EXE + ".part"))
    except OSError:
        pass  # missing (the normal case) or locked -- nothing to do


def download_ytdlp(progress_cb=None) -> str:
    """Download the latest yt-dlp into the cache dir; return its path.

    ``progress_cb(downloaded_bytes, total_bytes)`` is called as bytes arrive.
    Raises on network failure (the caller surfaces it).
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    dest = os.path.join(CACHE_DIR, _EXE)
    tmp = dest + ".part"
    req = urllib.request.Request(download_url(), headers={"User-Agent": "CueForge"})
    with _download_lock:
        # A concurrent caller may have finished the download while we waited on
        # the lock -- don't re-fetch (and don't touch its freshly-written file).
        if os.path.isfile(dest):
            return dest
        cleanup_partials()  # clear any orphan from a previously killed run
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(tmp, "wb") as out:
                    while True:
                        chunk = resp.read(_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)
            os.replace(tmp, dest)
            make_executable(dest)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    resolve_ytdlp.cache_clear()
    return dest


def ensure_ytdlp() -> str:
    """Resolve yt-dlp, downloading the latest release if none is available."""
    try:
        return resolve_ytdlp()
    except FileNotFoundError:
        return download_ytdlp()
