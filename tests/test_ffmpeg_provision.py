# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Tests for ffmpeg provisioning: cache-dir resolution, version parsing, and
the download/extract pipeline (network faked -- no real HTTP)."""

import hashlib
import io
import os
import zipfile

import pytest

from cueforge import ffmpeg_util


@pytest.fixture(autouse=True)
def _reset_provision_state(monkeypatch):
    """Isolate the module-level provision globals + resolver cache per test."""
    monkeypatch.setattr(ffmpeg_util, "_provision_started", False, raising=False)
    ffmpeg_util._provision_done.clear()
    ffmpeg_util._provision_state.update(
        phase="idle", percent=0, downloaded=0, total=0, version=None, error=None
    )
    ffmpeg_util._update_info.update(installed=None, latest=None, checked=False)
    ffmpeg_util.resolve_ffmpeg.cache_clear()
    monkeypatch.delenv("CUEFORGE_FFMPEG", raising=False)
    yield
    ffmpeg_util.resolve_ffmpeg.cache_clear()


def _make_release_zip() -> bytes:
    """A gyan-shaped release zip: binaries nested under <build>/bin/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ffmpeg-8.1.2-essentials_build/bin/ffmpeg" + _ext(), b"FFMPEG-BIN")
        zf.writestr("ffmpeg-8.1.2-essentials_build/bin/ffprobe" + _ext(), b"FFPROBE-BIN")
        zf.writestr("ffmpeg-8.1.2-essentials_build/README.txt", b"docs, ignored")
    return buf.getvalue()


def _ext() -> str:
    return ".exe" if os.name == "nt" else ""


class _FakeResponse:
    """Minimal urlopen() stand-in: context manager with headers + chunked read."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._buf.read(n)


# ---------------------------------------------------------------- resolve

def test_resolve_prefers_cache_dir(tmp_path, monkeypatch):
    cache = tmp_path / "bin"
    cache.mkdir()
    fake = cache / ffmpeg_util._EXE
    fake.write_bytes(b"x")
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    ffmpeg_util.resolve_ffmpeg.cache_clear()

    assert ffmpeg_util.resolve_ffmpeg() == str(fake)


def test_resolve_ffmpeg_never_blocks_on_download(monkeypatch):
    # Regression: /api/ffmpeg/status calls resolve on the event loop, so resolve
    # must NEVER wait on an in-flight download (that froze the whole server).
    monkeypatch.setattr(ffmpeg_util.os.path, "isfile", lambda p: False)  # nothing resolves
    monkeypatch.setattr(ffmpeg_util, "_provision_started", True, raising=False)

    class _Ev:
        def is_set(self):
            return False

        def wait(self, timeout=None):
            raise AssertionError("resolve_ffmpeg must not block on the download")

    monkeypatch.setattr(ffmpeg_util, "_provision_done", _Ev())
    ffmpeg_util.resolve_ffmpeg.cache_clear()
    with pytest.raises(FileNotFoundError):
        ffmpeg_util.resolve_ffmpeg()


def test_wait_for_ffmpeg_waits_only_when_downloading(monkeypatch):
    waited = {"n": 0}

    class _Ev:
        def is_set(self):
            return False

        def wait(self, timeout=None):
            waited["n"] += 1
            return True

    monkeypatch.setattr(ffmpeg_util, "_provision_done", _Ev())

    monkeypatch.setattr(ffmpeg_util, "_provision_started", False, raising=False)
    ffmpeg_util.wait_for_ffmpeg(timeout=1)
    assert waited["n"] == 0  # nothing downloading -> no wait

    monkeypatch.setattr(ffmpeg_util, "_provision_started", True, raising=False)
    ffmpeg_util.wait_for_ffmpeg(timeout=1)
    assert waited["n"] == 1  # in-flight download -> waits


# ---------------------------------------------------------------- version

def test_installed_version_parses_and_trims(monkeypatch):
    class _Proc:
        stdout = "ffmpeg version 8.1.2-essentials_build-www.gyan.dev\nbuilt with gcc"

    monkeypatch.setattr(ffmpeg_util.subprocess, "run", lambda *a, **k: _Proc())
    assert ffmpeg_util.installed_version("/fake/ffmpeg") == "8.1.2"


def test_installed_version_none_on_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("no exe")

    monkeypatch.setattr(ffmpeg_util.subprocess, "run", boom)
    assert ffmpeg_util.installed_version("/fake/ffmpeg") is None


# ---------------------------------------------------------------- extract

def test_extract_pulls_bin_only(tmp_path):
    zip_bytes = _make_release_zip()
    zip_path = tmp_path / "ff.zip"
    zip_path.write_bytes(zip_bytes)
    dest = tmp_path / "out"
    dest.mkdir()

    ffmpeg_util._extract_binaries(str(zip_path), str(dest))

    assert (dest / ffmpeg_util._EXE).read_bytes() == b"FFMPEG-BIN"
    assert (dest / ffmpeg_util._PROBE).read_bytes() == b"FFPROBE-BIN"
    assert not (dest / "README.txt").exists()


def test_extract_raises_without_ffmpeg(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("build/bin/notffmpeg", b"nope")
    zip_path = tmp_path / "bad.zip"
    zip_path.write_bytes(buf.getvalue())

    with pytest.raises(RuntimeError):
        ffmpeg_util._extract_binaries(str(zip_path), str(tmp_path))


# ---------------------------------------------------------------- download

def test_download_ffmpeg_end_to_end(tmp_path, monkeypatch):
    zip_bytes = _make_release_zip()
    cache = tmp_path / "bin"
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    monkeypatch.setattr(
        ffmpeg_util.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(zip_bytes)
    )
    # Correct checksum -> passes verification.
    monkeypatch.setattr(
        ffmpeg_util, "_fetch_text", lambda url: hashlib.sha256(zip_bytes).hexdigest()
    )

    seen = []
    path = ffmpeg_util.download_ffmpeg(lambda d, t: seen.append((d, t)))

    assert path == str(cache / ffmpeg_util._EXE)
    assert os.path.isfile(path)
    assert seen and seen[-1][0] == len(zip_bytes)          # progress reached 100%
    assert not (cache / (ffmpeg_util._ZIP_NAME + ".part")).exists()  # temp cleaned


def test_download_rejects_bad_checksum(tmp_path, monkeypatch):
    zip_bytes = _make_release_zip()
    cache = tmp_path / "bin"
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    monkeypatch.setattr(
        ffmpeg_util.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(zip_bytes)
    )
    monkeypatch.setattr(ffmpeg_util, "_fetch_text", lambda url: "deadbeef")

    with pytest.raises(RuntimeError, match="checksum"):
        ffmpeg_util.download_ffmpeg()
    assert not (cache / ffmpeg_util._EXE).exists()


def test_cleanup_partials_removes_orphans(tmp_path, monkeypatch):
    cache = tmp_path / "bin"
    cache.mkdir()
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    orphan = cache / (ffmpeg_util._ZIP_NAME + ".part")
    orphan.write_bytes(b"partial")
    extract_orphan = cache / (ffmpeg_util._EXE + ".part")
    extract_orphan.write_bytes(b"partial")
    keep = cache / ffmpeg_util._EXE          # a real (finished) binary must survive
    keep.write_bytes(b"FFMPEG-BIN")
    # A foreign tool's in-flight temp in the SHARED cache dir must NOT be touched
    # (guards against a regression to a blanket ``*.part`` glob).
    foreign = cache / "yt-dlp.exe.part"
    foreign.write_bytes(b"other-tool-downloading")

    ffmpeg_util.cleanup_partials()

    assert not orphan.exists()
    assert not extract_orphan.exists()
    assert keep.read_bytes() == b"FFMPEG-BIN"
    assert foreign.read_bytes() == b"other-tool-downloading"


def test_cleanup_partials_is_safe_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(tmp_path / "bin"))
    ffmpeg_util.cleanup_partials()  # dir does not even exist -> must not raise


# ---------------------------------------------------------------- provision

def test_provision_in_background_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(ffmpeg_util, "download_ffmpeg", lambda cb=None: calls.append(1) or "/x/ffmpeg")
    monkeypatch.setattr(ffmpeg_util, "installed_version", lambda p=None: "8.1.2")

    assert ffmpeg_util.provision_in_background() is True
    ffmpeg_util._provision_done.wait(timeout=5)
    # Second call is a no-op while one has already run.
    assert ffmpeg_util.provision_in_background() is False

    assert calls == [1]
    st = ffmpeg_util.get_provision_state()
    assert st["phase"] == "ready"
    assert st["version"] == "8.1.2"


def test_provision_records_error(monkeypatch):
    def boom(cb=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(ffmpeg_util, "download_ffmpeg", boom)

    ffmpeg_util.provision_in_background()
    ffmpeg_util._provision_done.wait(timeout=5)

    st = ffmpeg_util.get_provision_state()
    assert st["phase"] == "error"
    assert "network down" in st["error"]


# ---------------------------------------------------------------- update

def test_start_update_forces_after_a_prior_provision(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ffmpeg_util, "download_ffmpeg", lambda cb=None: calls.append(1) or "/x/ffmpeg"
    )
    monkeypatch.setattr(ffmpeg_util, "installed_version", lambda p=None: "8.2.0")

    assert ffmpeg_util.provision_in_background() is True
    ffmpeg_util._provision_done.wait(timeout=5)
    # A plain re-provision is a no-op, but an update forces a fresh download.
    assert ffmpeg_util.provision_in_background() is False
    assert ffmpeg_util.start_update() is True
    ffmpeg_util._provision_done.wait(timeout=5)

    assert calls == [1, 1]
    # The freshly installed version is recorded as current.
    assert ffmpeg_util.get_update_info()["installed"] == "8.2.0"


def test_check_versions_populates_info(monkeypatch):
    monkeypatch.setattr(ffmpeg_util, "installed_version", lambda p=None: "8.1.2")
    monkeypatch.setattr(ffmpeg_util, "latest_version", lambda: "8.2.0")

    info = ffmpeg_util.check_versions()
    assert info["installed"] == "8.1.2"
    assert info["latest"] == "8.2.0"
    assert ffmpeg_util.get_update_info()["checked"] is True


def test_update_available_logic():
    info = ffmpeg_util._update_info
    info.update(installed=None, latest=None)
    assert ffmpeg_util.update_available() is False           # nothing known yet

    info.update(installed="8.1.2", latest="8.1.2")
    assert ffmpeg_util.update_available() is False           # up to date

    info.update(installed="8.1.2", latest="8.2.0")
    assert ffmpeg_util.update_available() is True            # newer available
    assert ffmpeg_util.update_available(dismissed="8.2.0") is False  # dismissed
    assert ffmpeg_util.update_available(dismissed="8.1.9") is True    # stale dismiss
