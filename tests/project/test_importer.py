# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Import pipeline: dedup, clone, and format decoding."""

from __future__ import annotations

import os

import pytest

from cueforge.project import importer
from cueforge.project.storage import ProjectSession

from .conftest import wav_to_mp3, write_tone_wav


def test_import_new_then_duplicate(tmp_path):
    work = tmp_path / "work"
    session = ProjectSession.create_new(str(work), "Show")
    src = str(tmp_path / "tone.wav")
    write_tone_wav(src, seconds=0.5)

    r1 = importer.import_audio(session, src)
    assert r1.status == "new"
    assert r1.item is not None
    assert r1.item.name == "tone"
    assert r1.item.type == "normal"
    assert len(session.show.library) == 1
    assert session.has_audio(r1.audio_hash)

    # Re-importing the same bytes is a duplicate; nothing added.
    r2 = importer.import_audio(session, src)
    assert r2.status == "duplicate"
    assert r2.audio_hash == r1.audio_hash
    assert [m.id for m in r2.matches] == [r1.item.id]
    assert len(session.show.library) == 1


def test_import_different_source_is_new(tmp_path):
    work = tmp_path / "work"
    session = ProjectSession.create_new(str(work), "Show")
    a = str(tmp_path / "a.wav")
    b = str(tmp_path / "b.wav")
    write_tone_wav(a, seconds=0.5, freq=440.0)
    write_tone_wav(b, seconds=0.5, freq=880.0)

    r1 = importer.import_audio(session, a)
    r2 = importer.import_audio(session, b)
    assert r1.status == "new"
    assert r2.status == "new"
    assert r1.audio_hash != r2.audio_hash
    assert len(session.show.library) == 2


def test_add_clone_shares_audio_independent_params(tmp_path):
    work = tmp_path / "work"
    session = ProjectSession.create_new(str(work), "Show")
    src = str(tmp_path / "tone.wav")
    write_tone_wav(src, seconds=0.5)
    r1 = importer.import_audio(session, src)

    clone = importer.add_clone(session, r1.audio_hash, "Tone (copy)")
    assert clone.audio_hash == r1.audio_hash
    assert clone.id != r1.item.id
    assert len(session.show.library) == 2

    # Independent params: mutating clone does not touch the original.
    clone.gain_db = -6.0
    assert r1.item.gain_db == 0.0


def test_import_mp3_decodes(tmp_path):
    work = tmp_path / "work"
    session = ProjectSession.create_new(str(work), "Show")
    wav = str(tmp_path / "tone.wav")
    mp3 = str(tmp_path / "tone.mp3")
    write_tone_wav(wav, seconds=1.0)
    wav_to_mp3(wav, mp3)

    r = importer.import_audio(session, mp3)
    assert r.status == "new"
    assert session.has_audio(r.audio_hash)

    # Decoded stored FLAC should be roughly 1 second (mp3 padding tolerated).
    import soundfile as sf

    data, sr = sf.read(session.audio_path(r.audio_hash), always_2d=True)
    assert sr == 48_000
    dur = data.shape[0] / sr
    assert 0.9 < dur < 1.2


def test_decode_failure_rejected(tmp_path):
    work = tmp_path / "work"
    session = ProjectSession.create_new(str(work), "Show")
    bad = str(tmp_path / "bad.wav")
    with open(bad, "wb") as fh:
        fh.write(b"not audio at all")

    with pytest.raises(importer.ImportError):
        importer.import_audio(session, bad)
    # Rejected imports never enter the library.
    assert len(session.show.library) == 0
