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
relies on known-good behaviour -- notably ``libmp3lame``, which the MP3 export
path requires and which many distro builds omit. Instead, ffmpeg is a hard
prerequisite (every audio import decodes through it), so the launcher calls
:func:`provision_in_background` at startup when nothing is found: a known build
is fetched into the cache dir while the server boots. An import that races the
download blocks briefly (:func:`resolve_ffmpeg` waits on the in-flight
download) instead of failing outright.

Where that build comes from is per-platform (see :func:`_source`):

* **Windows** -- gyan.dev's ``ffmpeg-release-essentials.zip``, whose documented
  API publishes a ``.ver`` and ``.sha256`` beside each download.
* **Linux** -- the ``gpl`` static tarball from BtbN/FFmpeg-Builds, for x86_64
  or aarch64. There is no ``.ver`` equivalent, so the "latest" version is the
  newest release *branch* BtbN publishes (``n8.1`` -> ``"8.1"``) while the
  installed version stays the full ``ffmpeg -version`` string (``"8.1.2"``).
  Those are compared numerically rather than for equality, so a patch release
  ahead of its branch is correctly read as up to date.

Both sources are verified against a publisher-provided SHA-256 before anything
is extracted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from functools import lru_cache

from cueforge.platform_util import EXE_SUFFIX, IS_WINDOWS, arch_tag, make_executable

# Both borrowed from the app updater, which solves the same two problems on the
# same shapes of data: ``is_newer`` is a numeric "strictly greater" version
# comparison, and ``parse_checksums`` reads ``sha256sum``-format listings.
from cueforge.update_util import is_newer, parse_checksums

_EXE = "ffmpeg" + EXE_SUFFIX
_PROBE = "ffprobe" + EXE_SUFFIX

# Downloaded binaries live under the same writable home dir the server uses for
# config/projects/work (cueforge.server.app.HOME_DIR). Kept as a local literal
# so this low-level module never imports the server package.
_HOME_DIR = os.path.join(os.path.expanduser("~"), "CueForge")
CACHE_DIR = os.path.join(_HOME_DIR, "bin")

# The release archive streams to one fixed temp name regardless of platform or
# asset, so :func:`cleanup_partials` can name its own leftovers exactly -- it
# runs at startup and must not have to reach the network to learn an asset name.
_ARCHIVE_PART = "ffmpeg-download.part"

# gyan.dev documented API: the dot-prefixed files beside each download are the
# canonical per-package version (.ver) and checksum (.sha256).
_GYAN_BASE_URL = "https://www.gyan.dev/ffmpeg/builds"
_GYAN_ZIP_NAME = "ffmpeg-release-essentials.zip"

# BtbN/FFmpeg-Builds publishes every asset under one rolling ``latest`` tag,
# plus a single sha256sum-format ``checksums.sha256`` covering all of them.
# We take the ``gpl`` (not ``lgpl``) variant: lgpl builds omit libmp3lame.
_BTBN_REPO = "BtbN/FFmpeg-Builds"
_BTBN_RELEASE_URL = f"https://api.github.com/repos/{_BTBN_REPO}/releases/tags/latest"
_BTBN_DOWNLOAD_BASE = f"https://github.com/{_BTBN_REPO}/releases/download/latest"
_BTBN_SUMS_NAME = "checksums.sha256"
# Used when the GitHub API is unreachable, so a first-run download still works
# offline of the API. Any newer branch is discovered normally once it responds.
_BTBN_FALLBACK_BRANCH = "8.1"
_BTBN_ARCH = {"x86_64": "linux64", "aarch64": "linuxarm64"}

_HTTP_TIMEOUT = 30  # per-read socket timeout (seconds)
_CHUNK = 1024 * 256


# --------------------------------------------------------------------------
# Where the build comes from (per platform)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    """The release archive to fetch for this platform, and how to verify it.

    ``sums_key`` names the entry to look up when ``sums_url`` serves a
    multi-asset ``sha256sum``-format listing; ``None`` means the whole body is
    one digest for this download.
    """

    url: str
    sums_url: str | None = None
    sums_key: str | None = None
    #: Version this source installs, when the asset name already states it.
    version: str | None = None


def _gyan_source() -> Source:
    return Source(
        url=f"{_GYAN_BASE_URL}/{_GYAN_ZIP_NAME}",
        sums_url=f"{_GYAN_BASE_URL}/{_GYAN_ZIP_NAME}.sha256",
    )


def _btbn_asset_name(branch: str, arch: str) -> str:
    """e.g. ``ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz``."""
    return f"ffmpeg-n{branch}-latest-{_BTBN_ARCH[arch]}-gpl-{branch}.tar.xz"


# Matches the pinned-branch assets and captures the branch, e.g. "8.1". The
# rolling ``master-latest`` assets deliberately do not match: their binaries
# report a build number ("N-121086-g...") rather than a comparable version.
_BTBN_ASSET_RE = re.compile(
    r"^ffmpeg-n(\d+(?:\.\d+)*)-latest-(linux64|linuxarm64)-gpl-\1\.tar\.xz$"
)

# Only successful lookups are cached; a failed one must not pin the fallback
# branch for the life of the process.
_btbn_branch_cache: dict[str, str] = {}


def _btbn_branches(arch: str) -> list[str]:
    """Release branches BtbN currently publishes a gpl build of, for ``arch``."""
    try:
        req = urllib.request.Request(
            _BTBN_RELEASE_URL,
            headers={"User-Agent": "CueForge", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            release = json.load(resp)
    except Exception:
        return []
    want = _BTBN_ARCH.get(arch)
    found = []
    for asset in release.get("assets") or []:
        m = _BTBN_ASSET_RE.match(str(asset.get("name") or ""))
        if m and m.group(2) == want:
            found.append(m.group(1))
    return found


def _btbn_latest_branch(arch: str) -> str:
    """Newest branch BtbN publishes for ``arch``; the pinned fallback offline."""
    cached = _btbn_branch_cache.get(arch)
    if cached:
        return cached
    branches = _btbn_branches(arch)
    if not branches:
        return _BTBN_FALLBACK_BRANCH
    newest = max(branches, key=lambda b: tuple(int(p) for p in b.split(".")))
    _btbn_branch_cache[arch] = newest
    return newest


def _btbn_source() -> Source:
    arch = arch_tag()
    if arch not in _BTBN_ARCH:
        raise RuntimeError(
            f"no prebuilt ffmpeg for this architecture ({arch}); "
            "set CUEFORGE_FFMPEG to a binary with libmp3lame support"
        )
    branch = _btbn_latest_branch(arch)
    name = _btbn_asset_name(branch, arch)
    return Source(
        url=f"{_BTBN_DOWNLOAD_BASE}/{name}",
        sums_url=f"{_BTBN_DOWNLOAD_BASE}/{_BTBN_SUMS_NAME}",
        sums_key=name,
        version=branch,
    )


def _source() -> Source:
    """The download source for this platform. May hit the network (Linux needs
    the release listing to pick a branch), so call it from a worker thread."""
    return _gyan_source() if IS_WINDOWS else _btbn_source()


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
# Version metadata
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
    """Latest version available from this platform's source.

    Windows reads gyan.dev's ``.ver`` endpoint, so this is an exact release
    ("8.1.2"). Linux has no such endpoint and reports the newest *branch* BtbN
    publishes ("8.1") -- coarser than the installed version, which is why
    :func:`update_available` compares numerically instead of for equality.
    """
    if IS_WINDOWS:
        return _fetch_text(f"{_GYAN_BASE_URL}/{_GYAN_ZIP_NAME}.ver")
    try:
        return _btbn_latest_branch(arch_tag())
    except Exception:
        return None


def installed_version(path: str | None = None) -> str | None:
    """Version of the installed ffmpeg (``"8.1.2"``), or None if unavailable.

    Parses the first ``ffmpeg -version`` line and keeps just the numeric part,
    so it compares cleanly against :func:`latest_version`. Both publishers wrap
    it differently -- gyan.dev reports ``8.1.2-essentials_build-...`` and BtbN
    ``n8.1.2-34-g9b6c8969e0-20260804`` -- hence a search rather than a match.
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
    num = _NUM_RE.search(m.group(1))
    return num.group(0) if num else m.group(1)


# --------------------------------------------------------------------------
# Download / extract
# --------------------------------------------------------------------------
def _wanted_member(name: str, wanted: set[str]) -> str | None:
    """The flat destination name for an archive entry we care about, else None.

    Both publishers nest the binaries under ``<build>/bin/``, so the directory
    prefix distinguishes the real executable from same-named files elsewhere in
    the archive.
    """
    base = os.path.basename(name)
    if base in wanted and "/bin/" in name.replace("\\", "/"):
        return base
    return None


def _read_binaries(archive_path: str, wanted: set[str]) -> dict[str, bytes]:
    """Read the wanted members out of the release archive, keyed by basename.

    Handles both shapes we download -- gyan.dev ships a ``.zip``, BtbN a
    ``.tar.xz`` -- dispatched by sniffing the file rather than by the running
    platform, so either can be exercised on any machine. Members are read by
    name (never ``extractall``), so a crafted archive cannot write outside the
    destination directory.
    """
    found: dict[str, bytes] = {}
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                base = _wanted_member(name, wanted)
                if base:
                    found[base] = zf.read(name)
        return found
    if tarfile.is_tarfile(archive_path):
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                base = _wanted_member(member.name, wanted)
                if not base:
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    raise RuntimeError(f"unreadable archive member {member.name}")
                found[base] = fh.read()
        return found
    raise RuntimeError("downloaded ffmpeg archive is neither a zip nor a tar")


def _extract_binaries(archive_path: str, dest_dir: str) -> None:
    """Extract just ffmpeg(.exe) (+ ffprobe if present) from the release archive.

    We pull only what the app shells out to and drop them flat into
    ``dest_dir``, discarding the rest of the build (docs, presets, ffplay).
    """
    members = _read_binaries(archive_path, {_EXE, _PROBE})
    if _EXE not in members:
        raise RuntimeError(f"{_EXE} not found in downloaded archive")
    for base, data in members.items():
        dest = os.path.join(dest_dir, base)
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
        make_executable(dest)


def cleanup_partials() -> None:
    """Delete leftover ``*.part`` temp files from interrupted ffmpeg downloads.

    A clean download removes its ``.part`` in a ``finally``; a *hard* kill
    (power loss, ``taskkill /F``) can orphan one. Called at startup so the
    cache dir never accumulates junk. Only touches ffmpeg's own temp names, so
    it is safe to run while an unrelated download (e.g. yt-dlp) is in flight.
    """
    partials = (
        _ARCHIVE_PART,               # the release archive being streamed
        _GYAN_ZIP_NAME + ".part",    # what CueForge <= 0.2.0 named it on Windows
        _EXE + ".part",              # extract temp for ffmpeg
        _PROBE + ".part",            # extract temp for ffprobe
    )
    for name in partials:
        try:
            os.remove(os.path.join(CACHE_DIR, name))
        except OSError:
            pass  # missing (the normal case) or locked -- nothing to do


def _expected_digest(source: Source) -> str | None:
    """The publisher's SHA-256 for ``source``, or None when unavailable.

    gyan.dev serves one digest per download; BtbN serves a single
    ``sha256sum``-format listing covering every asset in the release, so
    ``sums_key`` selects our line out of it.

    An unreachable endpoint returns None and the download proceeds unverified,
    which is the long-standing behaviour and keeps a first run working when the
    checksum host is briefly down. A listing that *is* served but does not name
    our asset is a different thing -- something is wrong with the release we are
    about to execute -- so that raises instead of quietly skipping the check.
    """
    if not source.sums_url:
        return None
    body = _fetch_text(source.sums_url)
    if not body:
        return None
    if source.sums_key is None:
        return body.split()[0].lower()
    digest = parse_checksums(body).get(source.sums_key)
    if not digest:
        raise RuntimeError(
            f"{source.sums_key} has no checksum entry in {_BTBN_SUMS_NAME} "
            "-- refusing to install an unverified ffmpeg"
        )
    return digest


def download_ffmpeg(progress_cb=None) -> str:
    """Download and install ffmpeg into the cache dir; return its path.

    ``progress_cb(downloaded_bytes, total_bytes)`` is called as bytes arrive
    (``total`` is 0 when the server omits Content-Length). Verifies the
    publisher's SHA-256 when available. Raises on network/checksum/extract
    failure. Blocking -- call from a worker thread.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cleanup_partials()  # clear any orphan from a previously killed run

    source = _source()
    expected = _expected_digest(source)

    tmp_archive = os.path.join(CACHE_DIR, _ARCHIVE_PART)
    sha = hashlib.sha256()
    req = urllib.request.Request(source.url, headers={"User-Agent": "CueForge"})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(tmp_archive, "wb") as out:
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

        _extract_binaries(tmp_archive, CACHE_DIR)
    finally:
        try:
            os.remove(tmp_archive)
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
    """True if the source offers a strictly newer build than the installed copy
    and it hasn't been dismissed for that version.

    Compared numerically rather than for equality, because the two sides are
    not always the same shape: on Linux ``latest`` is a release branch ("8.1")
    while ``installed`` is the full build version ("8.1.2"), and a patch ahead
    of its branch means up to date, not "update available".
    """
    installed, latest = _update_info["installed"], _update_info["latest"]
    if not (installed and latest) or latest == dismissed:
        return False
    return is_newer(latest, installed)


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
