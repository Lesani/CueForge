# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Export/transcode pipeline: WAV fallback, cue-param export, filenames.

These drive the real vendored ffmpeg (like the importer tests) against small
synthesized FLAC blobs written straight to a ProjectSession's audio store.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
import soundfile as sf

from cueforge.audio_format import SAMPLE_RATE
from cueforge.project import exporter
from cueforge.project.exporter import (
    EXPORT_FORMATS,
    ExportError,
    content_disposition,
    export_cue,
    transcode_to_wav,
)
from cueforge.project.model import make_library_item
from cueforge.project.storage import ProjectSession

# A valid (all-hex) content hash -- audio_path rejects anything else.
H = "a" * 64


def _write_flac(session: ProjectSession, audio_hash: str, seconds: float) -> None:
    n = int(round(seconds * SAMPLE_RATE))
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    wave = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    stereo = np.stack([wave, wave], axis=1)
    sf.write(session.audio_path(audio_hash), stereo, SAMPLE_RATE, format="FLAC")


@pytest.fixture
def session(tmp_path):
    return ProjectSession.create_new(str(tmp_path / "work"), "Show")


# ---------------------------------------------------------------- WAV fallback

def test_transcode_to_wav_writes_riff(session, tmp_path):
    _write_flac(session, H, 0.5)
    dst = str(tmp_path / "out.wav")
    transcode_to_wav(session.audio_path(H), dst)

    with open(dst, "rb") as fh:
        header = fh.read(12)
    assert header[:4] == b"RIFF"
    assert header[8:12] == b"WAVE"

    info = sf.info(dst)
    assert info.samplerate == SAMPLE_RATE
    assert info.channels == 2
    assert info.subtype == "PCM_16"


# ---------------------------------------------------------------- export trim

def test_export_applies_trim_duration(session, tmp_path):
    _write_flac(session, H, 2.0)
    item = make_library_item(
        "Cue", type="normal", audio_hash=H, duration=2.0,
        trim_in=0.5, trim_out=1.5,
    )
    dst = str(tmp_path / "trim.wav")
    export_cue(session, item, "wav", dst)

    info = sf.info(dst)
    # trimmed region is 1.5 - 0.5 = 1.0 s.
    assert abs(info.duration - 1.0) < 0.05


def test_export_with_gain_and_fades_runs(session, tmp_path):
    _write_flac(session, H, 2.0)
    item = make_library_item(
        "Cue", type="normal", audio_hash=H, duration=2.0,
        trim_in=0.25, trim_out=1.75, gain_db=-6.0, fade_in=0.2, fade_out=0.3,
    )
    dst = str(tmp_path / "faded.flac")
    export_cue(session, item, "flac", dst)
    assert os.path.isfile(dst)
    info = sf.info(dst)
    assert abs(info.duration - 1.5) < 0.05


def test_export_mp3(session, tmp_path):
    _write_flac(session, H, 1.0)
    item = make_library_item("Cue", type="normal", audio_hash=H, duration=1.0)
    dst = str(tmp_path / "out.mp3")
    export_cue(session, item, "mp3", dst)
    assert os.path.isfile(dst)
    assert os.path.getsize(dst) > 0
    assert "mp3" in EXPORT_FORMATS


# ---------------------------------------------------------------- rejections

def test_export_rejects_stop_item(session, tmp_path):
    item = make_library_item("Stop", type="stop")
    with pytest.raises(ExportError):
        export_cue(session, item, "wav", str(tmp_path / "x.wav"))


def test_export_rejects_bad_format(session, tmp_path):
    _write_flac(session, H, 0.5)
    item = make_library_item("Cue", type="normal", audio_hash=H, duration=0.5)
    with pytest.raises(ExportError):
        export_cue(session, item, "ogg", str(tmp_path / "x.ogg"))


# ---------------------------------------------------------------- filenames

def test_content_disposition_ascii_and_utf8():
    cd = content_disposition("Lift Tur", "mp3")
    assert cd.startswith("attachment; ")
    assert 'filename="Lift Tur.mp3"' in cd
    assert "filename*=UTF-8''" in cd


def test_content_disposition_umlaut_falls_back_to_ascii():
    cd = content_disposition("Lift Tür", "wav")
    # ASCII fallback must not contain the non-ASCII byte; umlaut -> "_".
    assert 'filename="Lift T_r.wav"' in cd
    # RFC 5987 form percent-encodes the UTF-8 name.
    assert "filename*=UTF-8''Lift%20T%C3%BCr.wav" in cd


def test_content_disposition_empty_name_falls_back_to_cue():
    cd = content_disposition("", "flac")
    assert 'filename="cue.flac"' in cd
