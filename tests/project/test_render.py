# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Render: trim, engine params, and normalize."""

from __future__ import annotations

import numpy as np

from cueforge.audio_format import SAMPLE_RATE, db_to_gain
from cueforge.project import importer
from cueforge.project import render
from cueforge.project.storage import ProjectSession

from .conftest import write_tone_wav


def test_load_cue_pcm_applies_trim(tmp_path):
    session = ProjectSession.create_new(str(tmp_path / "work"), "Show")
    src = str(tmp_path / "tone.wav")
    write_tone_wav(src, seconds=1.0, freq=440.0, amp=0.5)
    r = importer.import_audio(session, src)
    item = r.item

    # Full length ~= 1.0s.
    full = render.load_cue_pcm(session, item)
    assert full.shape[1] == 2
    assert abs(full.shape[0] - SAMPLE_RATE) <= 2

    # Trim to [0.25, 0.75) -> ~0.5s.
    item.trim_in = 0.25
    item.trim_out = 0.75
    trimmed = render.load_cue_pcm(session, item)
    expected = SAMPLE_RATE // 2
    assert abs(trimmed.shape[0] - expected) <= 2

    # Trimmed samples equal the corresponding slice of the full signal.
    start = int(round(0.25 * SAMPLE_RATE))
    seg = full[start : start + trimmed.shape[0]]
    assert np.allclose(trimmed, seg, atol=1e-4)


def test_cue_engine_params(tmp_path):
    session = ProjectSession.create_new(str(tmp_path / "work"), "Show")
    item = importer.import_audio(
        session, write_tone_wav(str(tmp_path / "t.wav"), seconds=0.3)
    ).item
    item.gain_db = -4.0
    item.fade_in = 1.0
    item.fade_out = 2.0
    item.fade_shape = "equalPower"
    item.loop = True
    params = render.cue_engine_params(item)
    assert params == {
        "gain_db": -4.0,
        "fade_in": 1.0,
        "fade_out": 2.0,
        "fade_shape": "equalPower",
        "loop": True,
    }


def test_normalize_sets_gain_to_target(tmp_path):
    session = ProjectSession.create_new(str(tmp_path / "work"), "Show")
    # Quiet tone: peak ~0.1 (-20 dBFS).
    src = write_tone_wav(str(tmp_path / "quiet.wav"), seconds=0.5, amp=0.1)
    item = importer.import_audio(session, src).item

    render.normalize(session, item, target_dbfs=-1.0)

    pcm = render.load_cue_pcm(session, item)
    peak = float(np.max(np.abs(pcm)))
    applied_peak = peak * db_to_gain(item.gain_db)
    applied_dbfs = 20.0 * np.log10(applied_peak)
    assert abs(applied_dbfs - (-1.0)) < 0.1
