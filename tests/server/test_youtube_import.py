# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""YouTube import: yt-dlp resolution + the streaming import orchestration.

The real yt-dlp download is monkeypatched out (no network); we verify that the
streaming generator emits progress phases and funnels the downloaded file
through the existing import_audio() pipeline to create a LibraryItem.

Driven with anyio directly against the module-level ``youtube_import_events``
generator so no HTTP client (httpx) dependency is needed.
"""

from __future__ import annotations

import os

import anyio
import numpy as np
import pytest
import soundfile as sf

from cueforge import ytdlp_util
from cueforge.audio_format import SAMPLE_RATE
from cueforge.project import youtube
from cueforge.project.storage import ProjectSession
from cueforge.server.app import youtube_import_events


def write_tone_wav(path, seconds=1.0, freq=440.0, amp=0.5, sr=SAMPLE_RATE):
    """Write a stereo sine tone WAV and return its path."""
    n = int(round(seconds * sr))
    t = np.arange(n, dtype=np.float64) / sr
    wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    stereo = np.stack([wave, wave], axis=1)
    sf.write(path, stereo, sr, subtype="FLOAT")
    return path


# ---------------------------------------------------------------- resolve

def test_resolve_ytdlp_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "yt-dlp.exe"
    fake.write_bytes(b"binary")
    monkeypatch.setenv("CUEFORGE_YTDLP", str(fake))
    ytdlp_util.resolve_ytdlp.cache_clear()
    try:
        assert ytdlp_util.resolve_ytdlp() == str(fake)
    finally:
        ytdlp_util.resolve_ytdlp.cache_clear()


def test_resolve_ytdlp_finds_vendored(tmp_path, monkeypatch):
    # vendor/yt-dlp/yt-dlp.exe is an optional local artifact (gitignored); skip
    # when a working tree doesn't have it.
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    vendored = os.path.join(repo_root, "vendor", "yt-dlp", ytdlp_util._EXE)
    if not os.path.isfile(vendored):
        pytest.skip("no vendored yt-dlp in this working tree")
    monkeypatch.delenv("CUEFORGE_YTDLP", raising=False)
    monkeypatch.setattr(ytdlp_util, "CACHE_DIR", str(tmp_path / "empty"))  # isolate
    monkeypatch.setattr(ytdlp_util.shutil, "which", lambda name: None)  # ignore PATH
    ytdlp_util.resolve_ytdlp.cache_clear()
    try:
        assert ytdlp_util.resolve_ytdlp() == vendored
    finally:
        ytdlp_util.resolve_ytdlp.cache_clear()


def test_resolve_ytdlp_prefers_cache(tmp_path, monkeypatch):
    cache = tmp_path / "bin"
    cache.mkdir()
    fake = cache / ytdlp_util._EXE
    fake.write_bytes(b"x")
    monkeypatch.delenv("CUEFORGE_YTDLP", raising=False)
    monkeypatch.setattr(ytdlp_util, "CACHE_DIR", str(cache))
    ytdlp_util.resolve_ytdlp.cache_clear()
    try:
        assert ytdlp_util.resolve_ytdlp() == str(fake)
    finally:
        ytdlp_util.resolve_ytdlp.cache_clear()


def test_download_ytdlp_writes_binary(tmp_path, monkeypatch):
    import io

    cache = tmp_path / "bin"
    monkeypatch.setattr(ytdlp_util, "CACHE_DIR", str(cache))

    class _Resp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)
            self.headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return self._buf.read(n)

    monkeypatch.setattr(
        ytdlp_util.urllib.request, "urlopen", lambda *a, **k: _Resp(b"YTDLP-BIN")
    )
    ytdlp_util.resolve_ytdlp.cache_clear()
    try:
        seen = []
        path = ytdlp_util.download_ytdlp(lambda d, t: seen.append((d, t)))
        assert path == str(cache / ytdlp_util._EXE)
        assert (cache / ytdlp_util._EXE).read_bytes() == b"YTDLP-BIN"
        assert seen and seen[-1][0] == len(b"YTDLP-BIN")
        assert not (cache / (ytdlp_util._EXE + ".part")).exists()
    finally:
        ytdlp_util.resolve_ytdlp.cache_clear()


def test_ensure_ytdlp_downloads_when_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CUEFORGE_YTDLP", raising=False)
    monkeypatch.setattr(ytdlp_util, "CACHE_DIR", str(tmp_path / "empty"))
    # Force resolve to miss (no env/cache/vendor/PATH), then fake the download.
    monkeypatch.setattr(ytdlp_util.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        ytdlp_util, "resolve_ytdlp",
        lambda: (_ for _ in ()).throw(FileNotFoundError("nope")),
    )
    called = []
    monkeypatch.setattr(
        ytdlp_util, "download_ytdlp", lambda: called.append(1) or "/dl/yt-dlp"
    )
    assert ytdlp_util.ensure_ytdlp() == "/dl/yt-dlp"
    assert called == [1]


def test_cleanup_partials_removes_orphan(tmp_path, monkeypatch):
    cache = tmp_path / "bin"
    cache.mkdir()
    monkeypatch.setattr(ytdlp_util, "CACHE_DIR", str(cache))
    orphan = cache / (ytdlp_util._EXE + ".part")
    orphan.write_bytes(b"partial")
    keep = cache / ytdlp_util._EXE           # finished binary must survive
    keep.write_bytes(b"YTDLP-BIN")
    # A foreign tool's in-flight temp in the SHARED cache dir must NOT be touched.
    foreign = cache / "ffmpeg-release-essentials.zip.part"
    foreign.write_bytes(b"ffmpeg-downloading")

    ytdlp_util.cleanup_partials()

    assert not orphan.exists()
    assert keep.read_bytes() == b"YTDLP-BIN"
    assert foreign.read_bytes() == b"ffmpeg-downloading"
    # Absent-file / missing-dir case must not raise either.
    monkeypatch.setattr(ytdlp_util, "CACHE_DIR", str(tmp_path / "nope"))
    ytdlp_util.cleanup_partials()


# ---------------------------------------------------------------- helpers

def _fake_download_factory(seconds=0.5, title="My Video"):
    async def fake_download(url, dest_dir):
        path = os.path.join(dest_dir, "vid.wav")
        write_tone_wav(path, seconds=seconds)
        yield {"type": "progress", "percent": 0.0}
        yield {"type": "progress", "percent": 50.0}
        yield {"type": "done", "path": path, "title": title}
    return fake_download


async def _collect(session, url, broadcast=None):
    events = []
    async for ev in youtube_import_events(session, url, broadcast=broadcast):
        events.append(ev)
    return events


@pytest.fixture
def session(tmp_path):
    return ProjectSession.create_new(str(tmp_path / "work"), "Show")


@pytest.fixture(autouse=True)
def _no_update(monkeypatch):
    async def fake_update():
        return None
    monkeypatch.setattr(youtube, "ensure_updated", fake_update)


# ---------------------------------------------------------------- streaming

def test_youtube_import_happy_path(session, monkeypatch):
    monkeypatch.setattr(youtube, "download_audio", _fake_download_factory())

    broadcasts = []

    async def broadcast():
        broadcasts.append(True)

    events = anyio.run(_collect, session, "https://youtu.be/abc", broadcast)

    phases = [e["phase"] for e in events]
    assert "updating" in phases
    assert "downloading" in phases
    assert "importing" in phases

    done = events[-1]
    assert done["phase"] == "done"
    assert done["status"] == "new"
    assert done["item"]["name"] == "My Video"
    assert done["item"]["duration"] > 0
    assert broadcasts == [True]  # broadcast fired once for the new item

    assert len(session.show.library) == 1
    assert next(iter(session.show.library.values())).name == "My Video"


def test_youtube_import_duplicate(session, monkeypatch):
    monkeypatch.setattr(youtube, "download_audio", _fake_download_factory())

    first = anyio.run(_collect, session, "https://youtu.be/abc")[-1]
    assert first["status"] == "new"

    # Same downloaded bytes on the second attempt -> duplicate, no new item.
    second = anyio.run(_collect, session, "https://youtu.be/abc")[-1]
    assert second["phase"] == "done"
    assert second["status"] == "duplicate"
    assert second["matches"]
    assert len(session.show.library) == 1


def test_youtube_import_download_error(session, monkeypatch):
    async def failing_download(url, dest_dir):
        raise youtube.YouTubeError("ERROR: Video unavailable")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(youtube, "download_audio", failing_download)

    events = anyio.run(_collect, session, "https://youtu.be/bad")
    err = events[-1]
    assert err["phase"] == "error"
    assert "unavailable" in err["detail"]
    assert len(session.show.library) == 0


def test_youtube_import_no_session(monkeypatch):
    monkeypatch.setattr(youtube, "download_audio", _fake_download_factory())
    events = anyio.run(_collect, None, "https://youtu.be/abc")
    assert events[-1]["phase"] == "error"


# ---------------------------------------------------------------- url check

def test_download_audio_rejects_bad_url():
    async def run():
        async for _ in youtube.download_audio("not-a-url", "."):
            pass
    with pytest.raises(youtube.YouTubeError):
        anyio.run(run)
