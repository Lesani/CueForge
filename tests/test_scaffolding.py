# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Sanity checks that the package, format constants, and ffmpeg resolver work."""

import os

import numpy as np

import cueforge
from cueforge import audio_format as fmt
from cueforge.ffmpeg_util import resolve_ffmpeg


def test_version():
    assert cueforge.__version__


def test_format_constants():
    assert fmt.SAMPLE_RATE == 48_000
    assert fmt.CHANNELS == 2
    assert fmt.NP_DTYPE is np.float32


def test_frame_second_roundtrip():
    assert fmt.seconds_to_frames(1.0) == 48_000
    assert fmt.frames_to_seconds(48_000) == 1.0


def test_db_gain_roundtrip():
    assert abs(fmt.db_to_gain(0.0) - 1.0) < 1e-9
    assert abs(fmt.db_to_gain(-6.0206) - 0.5) < 1e-3
    assert abs(fmt.gain_to_db(0.5) - (-6.0206)) < 1e-2


def test_silence_shape():
    s = fmt.silence(128)
    assert s.shape == (128, 2)
    assert s.dtype == np.float32
    assert not s.any()


def test_ffmpeg_resolves_and_runs():
    exe = resolve_ffmpeg()
    assert os.path.isfile(exe)
