# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Tests for ffmpeg provisioning: cache-dir resolution, version parsing, and
the download/extract pipeline (network faked -- no real HTTP).

The two publishers ship different archive shapes -- gyan.dev a ``.zip`` for
Windows, BtbN a ``.tar.xz`` for Linux -- and the extractor picks between them by
sniffing the file rather than by the running platform, so both are exercised on
every machine.
"""

import hashlib
import io
import os
import tarfile
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
    ffmpeg_util._btbn_branch_cache.clear()
    ffmpeg_util.resolve_ffmpeg.cache_clear()
    monkeypatch.delenv("CUEFORGE_FFMPEG", raising=False)
    yield
    ffmpeg_util._btbn_branch_cache.clear()
    ffmpeg_util.resolve_ffmpeg.cache_clear()


def _ext() -> str:
    return ".exe" if os.name == "nt" else ""


def _make_release_zip() -> bytes:
    """A gyan-shaped release zip: binaries nested under <build>/bin/."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ffmpeg-8.1.2-essentials_build/bin/ffmpeg" + _ext(), b"FFMPEG-BIN")
        zf.writestr("ffmpeg-8.1.2-essentials_build/bin/ffprobe" + _ext(), b"FFPROBE-BIN")
        zf.writestr("ffmpeg-8.1.2-essentials_build/README.txt", b"docs, ignored")
    return buf.getvalue()


def _make_release_tarxz() -> bytes:
    """A BtbN-shaped release tarball: binaries nested under <build>/bin/."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for rel, data in (
            ("bin/ffmpeg" + _ext(), b"FFMPEG-BIN"),
            ("bin/ffprobe" + _ext(), b"FFPROBE-BIN"),
            ("bin/ffplay" + _ext(), b"not something we shell out to"),
            ("README.txt", b"docs, ignored"),
            # A same-named file outside bin/ must not be mistaken for the real
            # binary -- the "/bin/" prefix is what disambiguates it.
            ("ffmpeg" + _ext(), b"DECOY"),
        ):
            info = tarfile.TarInfo("ffmpeg-n8.1-latest-linux64-gpl-8.1/" + rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _release_archive(kind: str) -> bytes:
    return _make_release_zip() if kind == "zip" else _make_release_tarxz()


#: Both archive shapes, so extract/download tests run against each regardless
#: of which platform the suite happens to be running on.
ARCHIVE_KINDS = ("zip", "tar.xz")


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


def test_installed_version_parses_the_btbn_build_string(monkeypatch):
    # BtbN prefixes the tag with "n" and appends commit/date, so the numeric
    # part has to be searched for rather than matched at the start.
    class _Proc:
        stdout = "ffmpeg version n8.1.2-34-g9b6c8969e0-20260804 Copyright (c) 2000-2026"

    monkeypatch.setattr(ffmpeg_util.subprocess, "run", lambda *a, **k: _Proc())
    assert ffmpeg_util.installed_version("/fake/ffmpeg") == "8.1.2"


# ---------------------------------------------------------------- source

def test_btbn_asset_name_per_arch():
    assert ffmpeg_util._btbn_asset_name("8.1", "x86_64") == (
        "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"
    )
    assert ffmpeg_util._btbn_asset_name("8.1", "aarch64") == (
        "ffmpeg-n8.1-latest-linuxarm64-gpl-8.1.tar.xz"
    )


def test_btbn_picks_the_newest_branch(monkeypatch):
    # Numeric, not lexicographic: "8.10" outranks "8.9", and the rolling
    # master-latest assets are ignored because their version is a build number.
    names = [
        "ffmpeg-n7.1-latest-linux64-gpl-7.1.tar.xz",
        "ffmpeg-n8.9-latest-linux64-gpl-8.9.tar.xz",
        "ffmpeg-n8.10-latest-linux64-gpl-8.10.tar.xz",
        "ffmpeg-master-latest-linux64-gpl.tar.xz",
        "ffmpeg-n9.0-latest-linuxarm64-gpl-9.0.tar.xz",   # other arch
        "ffmpeg-n8.1-latest-win64-gpl.zip",               # other platform
    ]
    monkeypatch.setattr(
        ffmpeg_util, "_btbn_branches",
        lambda arch: ["8.9", "8.10", "7.1"] if arch == "x86_64" else [],
    )
    assert ffmpeg_util._btbn_latest_branch("x86_64") == "8.10"
    # Sanity-check the asset regex the real lookup filters with.
    matched = [n for n in names if ffmpeg_util._BTBN_ASSET_RE.match(n)]
    assert "ffmpeg-master-latest-linux64-gpl.tar.xz" not in matched
    assert "ffmpeg-n8.1-latest-win64-gpl.zip" not in matched


def test_btbn_falls_back_when_the_listing_is_unreachable(monkeypatch):
    monkeypatch.setattr(ffmpeg_util, "_btbn_branches", lambda arch: [])
    assert ffmpeg_util._btbn_latest_branch("x86_64") == ffmpeg_util._BTBN_FALLBACK_BRANCH
    # A failed lookup must not be cached, or the fallback sticks for the
    # lifetime of the process even once the network comes back.
    assert "x86_64" not in ffmpeg_util._btbn_branch_cache


def test_btbn_rejects_an_unsupported_arch(monkeypatch):
    monkeypatch.setattr(ffmpeg_util, "arch_tag", lambda: "riscv64")
    with pytest.raises(RuntimeError, match="riscv64"):
        ffmpeg_util._btbn_source()


# ---------------------------------------------------------------- extract

@pytest.mark.parametrize("kind", ARCHIVE_KINDS)
def test_extract_pulls_bin_only(tmp_path, kind):
    archive = tmp_path / "ff.archive"
    archive.write_bytes(_release_archive(kind))
    dest = tmp_path / "out"
    dest.mkdir()

    ffmpeg_util._extract_binaries(str(archive), str(dest))

    assert (dest / ffmpeg_util._EXE).read_bytes() == b"FFMPEG-BIN"
    assert (dest / ffmpeg_util._PROBE).read_bytes() == b"FFPROBE-BIN"
    assert not (dest / "README.txt").exists()
    # Everything else in the build is discarded, decoys included.
    assert sorted(p.name for p in dest.iterdir()) == sorted(
        [ffmpeg_util._EXE, ffmpeg_util._PROBE]
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
@pytest.mark.parametrize("kind", ARCHIVE_KINDS)
def test_extract_marks_binaries_executable(tmp_path, kind):
    # Neither archive carries a usable mode, and we exec what we extract.
    archive = tmp_path / "ff.archive"
    archive.write_bytes(_release_archive(kind))

    ffmpeg_util._extract_binaries(str(archive), str(tmp_path))

    assert os.access(tmp_path / ffmpeg_util._EXE, os.X_OK)
    assert os.access(tmp_path / ffmpeg_util._PROBE, os.X_OK)


def test_extract_raises_without_ffmpeg(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("build/bin/notffmpeg", b"nope")
    zip_path = tmp_path / "bad.zip"
    zip_path.write_bytes(buf.getvalue())

    with pytest.raises(RuntimeError):
        ffmpeg_util._extract_binaries(str(zip_path), str(tmp_path))


def test_extract_rejects_a_non_archive(tmp_path):
    # e.g. an HTML error page served where the release asset was expected.
    bad = tmp_path / "oops.bin"
    bad.write_bytes(b"<html>504 Gateway Timeout</html>" * 8)

    with pytest.raises(RuntimeError, match="neither a zip nor a tar"):
        ffmpeg_util._extract_binaries(str(bad), str(tmp_path))


# ---------------------------------------------------------------- download

def _fake_source(archive_bytes: bytes, monkeypatch, *, sums_key=None):
    """Point download_ffmpeg at a fixed in-memory archive, whatever the host OS.

    Returns the digest the publisher would advertise for it, so callers can
    serve a matching (or deliberately mismatched) checksum body.
    """
    source = ffmpeg_util.Source(
        url="https://example.test/ffmpeg-archive",
        sums_url="https://example.test/sums",
        sums_key=sums_key,
    )
    monkeypatch.setattr(ffmpeg_util, "_source", lambda: source)
    monkeypatch.setattr(
        ffmpeg_util.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(archive_bytes),
    )
    return hashlib.sha256(archive_bytes).hexdigest()


@pytest.mark.parametrize("kind", ARCHIVE_KINDS)
def test_download_ffmpeg_end_to_end(tmp_path, monkeypatch, kind):
    archive = _release_archive(kind)
    cache = tmp_path / "bin"
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    digest = _fake_source(archive, monkeypatch)
    # Correct checksum -> passes verification.
    monkeypatch.setattr(ffmpeg_util, "_fetch_text", lambda url: digest)

    seen = []
    path = ffmpeg_util.download_ffmpeg(lambda d, t: seen.append((d, t)))

    assert path == str(cache / ffmpeg_util._EXE)
    assert os.path.isfile(path)
    assert seen and seen[-1][0] == len(archive)            # progress reached 100%
    assert not (cache / ffmpeg_util._ARCHIVE_PART).exists()  # temp cleaned


@pytest.mark.parametrize("kind", ARCHIVE_KINDS)
def test_download_rejects_bad_checksum(tmp_path, monkeypatch, kind):
    archive = _release_archive(kind)
    cache = tmp_path / "bin"
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    _fake_source(archive, monkeypatch)
    monkeypatch.setattr(ffmpeg_util, "_fetch_text", lambda url: "de" * 32)

    with pytest.raises(RuntimeError, match="checksum"):
        ffmpeg_util.download_ffmpeg()
    assert not (cache / ffmpeg_util._EXE).exists()
    assert not (cache / ffmpeg_util._ARCHIVE_PART).exists()


def test_download_rejects_listing_without_our_asset(tmp_path, monkeypatch):
    # BtbN serves one listing for every asset in the release. Reaching it but
    # not finding our line means something is wrong with the release -- that
    # must not silently degrade into an unverified install.
    archive = _make_release_tarxz()
    cache = tmp_path / "bin"
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    _fake_source(archive, monkeypatch, sums_key="ffmpeg-n9.9-latest-linux64-gpl-9.9.tar.xz")
    monkeypatch.setattr(
        ffmpeg_util, "_fetch_text",
        lambda url: f"{'ab' * 32}  some-other-asset.tar.xz\n",
    )

    with pytest.raises(RuntimeError, match="unverified"):
        ffmpeg_util.download_ffmpeg()
    assert not (cache / ffmpeg_util._EXE).exists()


def test_download_selects_our_line_from_a_multi_asset_listing(tmp_path, monkeypatch):
    archive = _make_release_tarxz()
    cache = tmp_path / "bin"
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    ours = "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz"
    digest = _fake_source(archive, monkeypatch, sums_key=ours)
    monkeypatch.setattr(
        ffmpeg_util, "_fetch_text",
        lambda url: (
            f"{'11' * 32}  ffmpeg-n8.1-latest-win64-gpl.zip\n"
            f"{digest}  {ours}\n"
            f"{'22' * 32}  ffmpeg-n8.1-latest-linuxarm64-gpl-8.1.tar.xz\n"
        ),
    )

    assert ffmpeg_util.download_ffmpeg() == str(cache / ffmpeg_util._EXE)


def test_download_tolerates_an_unreachable_checksum_endpoint(tmp_path, monkeypatch):
    # Long-standing behaviour: a checksum host that is briefly down must not
    # block a first run, so the download proceeds unverified.
    archive = _make_release_zip()
    cache = tmp_path / "bin"
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    _fake_source(archive, monkeypatch)
    monkeypatch.setattr(ffmpeg_util, "_fetch_text", lambda url: None)

    assert ffmpeg_util.download_ffmpeg() == str(cache / ffmpeg_util._EXE)


def test_cleanup_partials_removes_orphans(tmp_path, monkeypatch):
    cache = tmp_path / "bin"
    cache.mkdir()
    monkeypatch.setattr(ffmpeg_util, "CACHE_DIR", str(cache))
    orphan = cache / ffmpeg_util._ARCHIVE_PART
    orphan.write_bytes(b"partial")
    legacy = cache / (ffmpeg_util._GYAN_ZIP_NAME + ".part")  # named by <= 0.2.0
    legacy.write_bytes(b"partial")
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


def test_update_available_compares_a_branch_against_a_patch_release():
    # On Linux the two sides are different shapes: "latest" is the newest
    # release branch BtbN publishes, "installed" is the full build version. A
    # patch ahead of its own branch is up to date, not an available update --
    # an equality check here would nag on every startup forever.
    info = ffmpeg_util._update_info

    info.update(installed="8.1.2", latest="8.1")
    assert ffmpeg_util.update_available() is False

    info.update(installed="8.1", latest="8.1")
    assert ffmpeg_util.update_available() is False

    # A genuinely newer branch still registers.
    info.update(installed="8.1.2", latest="8.2")
    assert ffmpeg_util.update_available() is True

    # And a source that somehow reports older than what is installed does not
    # offer a "downgrade update".
    info.update(installed="8.2.0", latest="8.1")
    assert ffmpeg_util.update_available() is False
