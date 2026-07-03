# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Locate -- and, when missing, download -- the ffmpeg binary used for decoding
imported audio.

Resolution order (first hit wins):
  1. CUEFORGE_FFMPEG env var (explicit override)
  2. Downloaded cache (~/CueForge/bin/ffmpeg[.exe])
  3. Frozen build: vendored binary beside the exe
     (<exe dir>/vendored/ffmpeg/ffmpeg[.exe])
  4. Vendored binary (repo_root/vendor/ffmpeg/ffmpeg[.exe])

We deliberately do NOT fall back to a system ffmpeg (PATH) or imageio-ffmpeg:
those could be arbitrary builds with different codecs/filters, and CueForge
relies on known-good behaviour. Instead, ffmpeg is a hard prerequisite (every
audio import decodes through it), so the launcher calls
:func:`provision_in_background` at startup when nothing is found: a known build
is fetched from gyan.dev into the cache dir while the server boots. An import
that races the download blocks briefly (:func:`resolve_ffmpeg` waits on the
in-flight download) instead of failing outright.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import urllib.request
import zipfile
from functools import lru_cache

_EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
_PROBE = "ffprobe.exe" if os.name == "nt" else "ffprobe"

# Downloaded binaries live under the same writable home dir the server uses for
# config/projects/work (cueforge.server.app.HOME_DIR). Kept as a local literal
# so this low-level module never imports the server package.
_HOME_DIR = os.path.join(os.path.expanduser("~"), "CueForge")
CACHE_DIR = os.path.join(_HOME_DIR, "bin")

# gyan.dev documented API: the dot-prefixed files beside each download are the
# canonical per-package version (.ver) and checksum (.sha256).
_BASE_URL = "https://www.gyan.dev/ffmpeg/builds"
_ZIP_NAME = "ffmpeg-release-essentials.zip"
DOWNLOAD_URL = f"{_BASE_URL}/{_ZIP_NAME}"
VERSION_URL = f"{_BASE_URL}/{_ZIP_NAME}.ver"
SHA256_URL = f"{_BASE_URL}/{_ZIP_NAME}.sha256"

_HTTP_TIMEOUT = 30  # per-read socket timeout (seconds)
_CHUNK = 1024 * 256


@lru_cache(maxsize=1)
def resolve_ffmpeg() -> str:
    """Return an absolute path to a usable ffmpeg binary, or raise."""
    env = os.environ.get("CUEFORGE_FFMPEG")
    if env and os.path.isfile(env):
        return env

    cached = os.path.join(CACHE_DIR, _EXE)
    if os.path.isfile(cached):
        return cached

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cand = os.path.join(exe_dir, "vendored", "ffmpeg", _EXE)
        if os.path.isfile(cand):
            return cand

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cand = os.path.join(repo_root, "vendor", "ffmpeg", _EXE)
    if os.path.isfile(cand):
        return cand

    # Deliberately no PATH / imageio-ffmpeg fallback -- we only use a copy we
    # control. NOTE: this function is pure and non-blocking (it is called from
    # async request handlers on the event loop, e.g. /api/ffmpeg/status). To
    # wait for an in-flight startup download, use wait_for_ffmpeg() from a
    # worker thread -- never block here.
    raise FileNotFoundError(
        "ffmpeg not found. Set CUEFORGE_FFMPEG, place a binary in "
        "vendor/ffmpeg/, or let CueForge download one on startup."
    )


def wait_for_ffmpeg(timeout: float = 300) -> None:
    """Block until an in-flight startup download finishes, then refresh the
    resolver cache. Best-effort and a no-op when nothing is downloading.

    MUST be called only from a worker thread (import/export run via
    asyncio.to_thread) -- never from the asyncio event loop, or it freezes the
    whole server for the duration of the download.
    """
    if _provision_started and not _provision_done.is_set():
        _provision_done.wait(timeout=timeout)
        resolve_ffmpeg.cache_clear()


# --------------------------------------------------------------------------
# Version metadata (gyan.dev API)
# --------------------------------------------------------------------------
_VER_RE = re.compile(r"ffmpeg version (\S+)")
_NUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*")


def _fetch_text(url: str) -> str | None:
    """GET a small text endpoint, stripped; None on any failure/offline."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CueForge"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace").strip()
    except Exception:
        return None


def latest_version() -> str | None:
    """Latest release version string from gyan.dev (e.g. ``"8.1.2"``)."""
    return _fetch_text(VERSION_URL)


def installed_version(path: str | None = None) -> str | None:
    """Version of the installed ffmpeg (``"8.1.2"``), or None if unavailable.

    Parses the first ``ffmpeg -version`` line and trims the build suffix so it
    compares cleanly against :func:`latest_version`.
    """
    exe = path
    if exe is None:
        try:
            exe = resolve_ffmpeg()
        except Exception:
            return None
    try:
        out = subprocess.run(
            [exe, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=10,
        )
    except Exception:
        return None
    m = _VER_RE.search(out.stdout or "")
    if not m:
        return None
    num = _NUM_RE.match(m.group(1))  # "8.1.2-essentials_build-..." -> "8.1.2"
    return num.group(0) if num else m.group(1)


# --------------------------------------------------------------------------
# Download / extract
# --------------------------------------------------------------------------
def _extract_binaries(zip_path: str, dest_dir: str) -> None:
    """Extract just ffmpeg(.exe) (+ ffprobe if present) from the release zip.

    The archive nests binaries under ``<build>/bin/``; we pull only what the
    app shells out to and drop them flat into ``dest_dir``.
    """
    wanted = {_EXE, _PROBE}
    with zipfile.ZipFile(zip_path) as zf:
        members: dict[str, str] = {}
        for name in zf.namelist():
            base = os.path.basename(name)
            if base in wanted and "/bin/" in name.replace("\\", "/"):
                members[base] = name
        if _EXE not in members:
            raise RuntimeError(f"{_EXE} not found in downloaded archive")
        for base, name in members.items():
            data = zf.read(name)
            dest = os.path.join(dest_dir, base)
            tmp = dest + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, dest)
            if os.name != "nt":
                os.chmod(dest, 0o755)


def cleanup_partials() -> None:
    """Delete leftover ``*.part`` temp files from interrupted ffmpeg downloads.

    A clean download removes its ``.part`` in a ``finally``; a *hard* kill
    (power loss, ``taskkill /F``) can orphan one. Called at startup so the
    cache dir never accumulates junk. Only touches ffmpeg's own temp names, so
    it is safe to run while an unrelated download (e.g. yt-dlp) is in flight.
    """
    partials = (
        _ZIP_NAME + ".part",   # the release archive being streamed
        _EXE + ".part",        # extract temp for ffmpeg
        _PROBE + ".part",      # extract temp for ffprobe
    )
    for name in partials:
        try:
            os.remove(os.path.join(CACHE_DIR, name))
        except OSError:
            pass  # missing (the normal case) or locked -- nothing to do


def download_ffmpeg(progress_cb=None) -> str:
    """Download and install ffmpeg into the cache dir; return its path.

    ``progress_cb(downloaded_bytes, total_bytes)`` is called as bytes arrive
    (``total`` is 0 when the server omits Content-Length). Verifies the
    gyan.dev SHA-256 when available. Raises on network/checksum/extract failure.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cleanup_partials()  # clear any orphan from a previously killed run

    expected = _fetch_text(SHA256_URL)
    expected = expected.split()[0].lower() if expected else None

    tmp_zip = os.path.join(CACHE_DIR, _ZIP_NAME + ".part")
    sha = hashlib.sha256()
    req = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": "CueForge"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(tmp_zip, "wb") as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    sha.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)

        if expected and sha.hexdigest().lower() != expected:
            raise RuntimeError("ffmpeg download failed checksum verification")

        _extract_binaries(tmp_zip, CACHE_DIR)
    finally:
        try:
            os.remove(tmp_zip)
        except OSError:
            pass

    resolve_ffmpeg.cache_clear()
    return os.path.join(CACHE_DIR, _EXE)


def is_available() -> bool:
    """True if a usable ffmpeg resolves right now (no download wait)."""
    try:
        resolve_ffmpeg()
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Background provisioning (startup download + on-demand update)
# --------------------------------------------------------------------------
_provision_lock = threading.Lock()
_provision_done = threading.Event()
_provision_started = False
_provision_state: dict = {
    "phase": "idle",   # idle | downloading | ready | error
    "percent": 0,
    "downloaded": 0,
    "total": 0,
    "version": None,
    "error": None,
}

# Version check populated in the background so status requests never block on
# the network. ``installed`` tracks the resolved binary; ``latest`` is gyan.dev.
_update_info: dict = {"installed": None, "latest": None, "checked": False}


def get_provision_state() -> dict:
    """A snapshot of the current download/provision state (safe to read)."""
    return dict(_provision_state)


def get_update_info() -> dict:
    """A snapshot of the installed/latest version check (safe to read)."""
    return dict(_update_info)


def update_available(dismissed: str | None = None) -> bool:
    """True if the latest release differs from the installed copy and hasn't
    been dismissed for that version."""
    installed, latest = _update_info["installed"], _update_info["latest"]
    return bool(installed and latest and installed != latest and latest != dismissed)


def _provision_worker() -> None:
    def cb(downloaded: int, total: int) -> None:
        _provision_state.update(
            phase="downloading",
            downloaded=downloaded,
            total=total,
            percent=int(downloaded * 100 / total) if total else 0,
        )

    try:
        path = download_ffmpeg(cb)
        version = installed_version(path)
        _provision_state.update(phase="ready", percent=100, version=version, error=None)
        _update_info["installed"] = version  # freshly installed copy is current
    except Exception as exc:  # noqa: BLE001 -- surfaced via state, never crashes boot
        _provision_state.update(phase="error", error=str(exc))
    finally:
        _provision_done.set()


def _begin_download(force: bool) -> bool:
    """Start a background download. Returns False if one is already running, or
    if a provision already ran and ``force`` is not set."""
    global _provision_started
    with _provision_lock:
        if _provision_state["phase"] == "downloading":
            return False
        if _provision_started and not force:
            return False
        _provision_started = True
        _provision_done.clear()
        _provision_state.update(
            phase="downloading", percent=0, downloaded=0, total=0, error=None
        )
    threading.Thread(
        target=_provision_worker, name="cueforge-ffmpeg-dl", daemon=True
    ).start()
    return True


def provision_in_background() -> bool:
    """Start the one-shot startup download (idempotent -- a no-op once run)."""
    return _begin_download(force=False)


def start_update() -> bool:
    """Force a fresh download to update ffmpeg. Returns False if a download is
    already in progress."""
    return _begin_download(force=True)


def check_versions() -> dict:
    """Populate installed + latest versions (both hit external resources)."""
    _update_info["installed"] = installed_version()
    _update_info["latest"] = latest_version()
    _update_info["checked"] = True
    return dict(_update_info)


def check_versions_in_background() -> None:
    """Run :func:`check_versions` off the request/boot path."""
    threading.Thread(
        target=check_versions, name="cueforge-ffmpeg-ver", daemon=True
    ).start()
