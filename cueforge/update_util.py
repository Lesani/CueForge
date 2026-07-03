# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Application self-update from GitHub Releases.

Checking: :func:`check_now` asks the GitHub API for the latest release of
``Lesani/CueForge`` and compares its tag against the running
``cueforge.__version__``. The launcher starts a periodic background check
(gated on the ``checkForUpdates`` setting) so ``/api/update/status`` can answer
without ever blocking on the network.

Applying (frozen builds only): the release's ``CueForge.exe`` asset is
downloaded next to the running exe as ``CueForge.exe.new`` and verified against
the release's ``SHA256SUMS.txt``. Then a rename-swap: Windows locks a running
exe against delete/write but NOT rename, so the running file is renamed to
``CueForge.exe.old`` and the download takes its place -- no helper process or
batch script. The worker then asks the server to shut down gracefully; the
launcher sees :func:`restart_pending` after the server loop exits (port
already free) and spawns the new exe. Leftover ``.old``/``.new`` files are
swept on the next startups.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

from cueforge import __version__

GITHUB_REPO = "Lesani/CueForge"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "CueForge.exe"
CHECKSUMS_NAME = "SHA256SUMS.txt"

CHECK_INTERVAL = 12 * 3600  # periodic re-check cadence (seconds)
_HTTP_TIMEOUT = 30          # per-read socket timeout (seconds)
_CHUNK = 1024 * 256


# --------------------------------------------------------------------------
# Version comparison
# --------------------------------------------------------------------------
_NUM_RE = re.compile(r"\d+(?:\.\d+)*")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def parse_version(text: str) -> tuple[int, ...] | None:
    """``"v0.2.10"`` -> ``(0, 2, 10)``; None when no numeric part is found."""
    m = _NUM_RE.search(text or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(0).split("."))


def is_newer(latest: str | None, current: str | None) -> bool:
    """True if ``latest`` is a strictly higher version than ``current``.

    Unknown/unparseable versions never report an update (fail quiet: a bad
    tag must not nag every operator to "update" to garbage).
    """
    lv = parse_version(latest or "")
    cv = parse_version(current or "")
    if lv is None or cv is None:
        return False
    width = max(len(lv), len(cv))
    return lv + (0,) * (width - len(lv)) > cv + (0,) * (width - len(cv))


# --------------------------------------------------------------------------
# Release check (GitHub API)
# --------------------------------------------------------------------------
# Populated in the background so status requests never block on the network.
_check_info: dict = {
    "current": __version__,
    "latest": None,        # latest release version, tag stripped of "v"
    "url": None,           # release page (html_url) for manual downloads
    "assetUrl": None,      # browser_download_url of the CueForge.exe asset
    "assetSize": 0,
    "checksumsUrl": None,  # browser_download_url of SHA256SUMS.txt
    "checked": False,
    "error": None,
}


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={"User-Agent": "CueForge", "Accept": "application/vnd.github+json"},
    )


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(_request(url), timeout=_HTTP_TIMEOUT) as resp:
        return json.load(resp)


def _fetch_text(url: str) -> str | None:
    """GET a small text endpoint, stripped; None on any failure/offline."""
    try:
        with urllib.request.urlopen(_request(url), timeout=_HTTP_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace").strip()
    except Exception:
        return None


def _extract_release(release: dict) -> dict:
    """Pull the fields we need out of a GitHub release JSON (pure/testable)."""
    tag = str(release.get("tag_name") or "").strip()
    info = {
        "latest": tag.lstrip("vV") or None,
        "url": release.get("html_url"),
        "assetUrl": None,
        "assetSize": 0,
        "checksumsUrl": None,
    }
    for asset in release.get("assets") or []:
        name = asset.get("name")
        if name == ASSET_NAME:
            info["assetUrl"] = asset.get("browser_download_url")
            info["assetSize"] = int(asset.get("size") or 0)
        elif name == CHECKSUMS_NAME:
            info["checksumsUrl"] = asset.get("browser_download_url")
    return info


def check_now() -> dict:
    """Query GitHub for the latest release; record result (or error) and
    return a snapshot. Blocking -- call from a worker thread."""
    try:
        info = _extract_release(_fetch_json(LATEST_RELEASE_URL))
        _check_info.update(checked=True, error=None, **info)
    except Exception as exc:  # noqa: BLE001 -- offline/rate-limited is normal
        _check_info.update(checked=True, error=str(exc))
    return dict(_check_info)


def get_check_info() -> dict:
    """A snapshot of the last release check (safe to read, never blocks)."""
    return dict(_check_info)


def update_available() -> bool:
    """True if the latest GitHub release is newer than the running version."""
    return is_newer(_check_info["latest"], _check_info["current"])


_periodic_started = False


def start_periodic_checks(enabled, interval: float = CHECK_INTERVAL) -> None:
    """Start the background check loop (idempotent).

    ``enabled`` is a zero-arg callable read before every check so the
    ``checkForUpdates`` setting takes effect without a restart. The first
    check runs immediately (when enabled), then every ``interval`` seconds.
    """
    global _periodic_started
    if _periodic_started:
        return
    _periodic_started = True

    def loop() -> None:
        while True:
            try:
                if enabled():
                    check_now()
            except Exception:
                pass  # keep the loop alive; next cycle retries
            time.sleep(interval)

    threading.Thread(target=loop, name="cueforge-update-check", daemon=True).start()


# --------------------------------------------------------------------------
# Apply (download + rename-swap + restart request)
# --------------------------------------------------------------------------
_apply_lock = threading.Lock()
_apply_state: dict = {
    "phase": "idle",   # idle | downloading | restarting | error
    "percent": 0,
    "downloaded": 0,
    "total": 0,
    "error": None,
}
_restart_pending = False


def get_apply_state() -> dict:
    """A snapshot of the current apply/download state (safe to read)."""
    return dict(_apply_state)


def can_apply() -> bool:
    """Self-update only works for the packaged exe; from source it's git."""
    return bool(getattr(sys, "frozen", False))


def restart_pending() -> bool:
    """True once an update is installed and the launcher should hand over."""
    return _restart_pending


def _exe_path() -> str:
    return os.path.abspath(sys.executable)


def parse_checksums(text: str) -> dict[str, str]:
    """Parse ``sha256sum``-format lines (``<hex>  [*]<name>``) to {name: hex}."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and _SHA256_RE.fullmatch(parts[0]):
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


def _download(url: str, dest: str, expected_size: int = 0) -> str:
    """Stream ``url`` to ``dest`` (via a ``.part`` temp), updating the apply
    state as bytes arrive. Returns the SHA-256 hex digest of the payload."""
    tmp = dest + ".part"
    sha = hashlib.sha256()
    try:
        with urllib.request.urlopen(_request(url), timeout=_HTTP_TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length") or expected_size or 0)
            downloaded = 0
            with open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    sha.update(chunk)
                    downloaded += len(chunk)
                    _apply_state.update(
                        downloaded=downloaded,
                        total=total,
                        percent=int(downloaded * 100 / total) if total else 0,
                    )
        os.replace(tmp, dest)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return sha.hexdigest()


def _swap_exe(exe_path: str, new_path: str) -> None:
    """Rename-swap the running exe for the downloaded one.

    Windows locks a running exe against delete/overwrite but allows renaming
    it, so: current -> ``.old``, download -> current. On any failure the
    original is renamed back so the installation stays runnable.
    """
    old_path = exe_path + ".old"
    try:
        os.remove(old_path)  # stale leftover from an earlier update
    except OSError:
        pass
    os.rename(exe_path, old_path)
    try:
        os.replace(new_path, exe_path)
    except Exception:
        os.rename(old_path, exe_path)
        raise


def _apply_worker(request_shutdown) -> None:
    global _restart_pending
    info = dict(_check_info)
    exe = _exe_path()
    new_path = exe + ".new"
    try:
        # Resolve the expected hash BEFORE downloading; verification is
        # mandatory, so a release without a usable SHA256SUMS.txt is refused
        # rather than installed unverified.
        expected = None
        if info.get("checksumsUrl"):
            sums = parse_checksums(_fetch_text(info["checksumsUrl"]) or "")
            expected = sums.get(ASSET_NAME)
        if not expected:
            raise RuntimeError(
                "release has no usable SHA256SUMS.txt -- refusing unverified update"
            )
        digest = _download(info["assetUrl"], new_path, info.get("assetSize") or 0)
        if digest.lower() != expected:
            raise RuntimeError("update download failed checksum verification")
        _swap_exe(exe, new_path)
        _restart_pending = True
        _apply_state.update(phase="restarting", percent=100, error=None)
        time.sleep(2)  # let clients observe "restarting" in one last poll
        request_shutdown()
    except Exception as exc:  # noqa: BLE001 -- surfaced via state, server keeps running
        try:
            os.remove(new_path)
        except OSError:
            pass
        _apply_state.update(phase="error", error=str(exc))


def start_apply(request_shutdown) -> bool:
    """Start the background download-and-swap; False if one is already
    running or there is nothing applicable. ``request_shutdown`` is called
    once the swap succeeded and must stop the server loop gracefully."""
    if not can_apply():
        return False
    with _apply_lock:
        if _apply_state["phase"] in ("downloading", "restarting"):
            return False
        if not (_check_info.get("assetUrl") and update_available()):
            return False
        _apply_state.update(
            phase="downloading", percent=0, downloaded=0, total=0, error=None
        )
    threading.Thread(
        target=_apply_worker, args=(request_shutdown,),
        name="cueforge-update-apply", daemon=True,
    ).start()
    return True


def spawn_replacement() -> None:
    """Launch the freshly installed exe. Called by the launcher AFTER the
    server loop exits (so the port is already free). The operator's browser
    tab reloads itself, so the new instance must not open another one."""
    exe = _exe_path()
    env = dict(os.environ)
    env["CUEFORGE_NO_BROWSER"] = "1"
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    try:
        subprocess.Popen(
            [exe], cwd=os.path.dirname(exe) or None, env=env,
            close_fds=True, **kwargs,
        )
    except Exception:
        pass  # the swap already happened; worst case the operator restarts by hand


def cleanup_leftovers() -> None:
    """Sweep ``.old``/``.new``/``.part`` files a previous self-update left
    beside the exe. Right after an update the predecessor process may still
    be exiting and hold its (renamed) image locked, so retry briefly in the
    background; whatever stays locked is removed on a later startup."""
    if not can_apply():
        return
    exe = _exe_path()
    paths = [
        p for p in (exe + ".old", exe + ".new", exe + ".new.part")
        if os.path.exists(p)
    ]
    if not paths:
        return

    def sweep(pending: list[str]) -> None:
        deadline = time.monotonic() + 30
        while pending:
            remaining = []
            for p in pending:
                try:
                    os.remove(p)
                except OSError:
                    if os.path.exists(p):
                        remaining.append(p)
            pending = remaining
            if not pending or time.monotonic() > deadline:
                return
            time.sleep(2)

    threading.Thread(
        target=sweep, args=(paths,), name="cueforge-update-sweep", daemon=True
    ).start()
