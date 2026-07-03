# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Shared fixtures/helpers for project tests: synthesize audio via numpy/sf/ffmpeg."""

from __future__ import annotations

import os
import subprocess

import numpy as np
import soundfile as sf

from cueforge.audio_format import SAMPLE_RATE
from cueforge.ffmpeg_util import resolve_ffmpeg


def write_tone_wav(
    path: str,
    seconds: float = 1.0,
    freq: float = 440.0,
    amp: float = 0.5,
    sr: int = SAMPLE_RATE,
) -> str:
    """Write a stereo sine tone WAV and return its path."""
    n = int(round(seconds * sr))
    t = np.arange(n, dtype=np.float64) / sr
    wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    stereo = np.stack([wave, wave], axis=1)
    sf.write(path, stereo, sr, subtype="FLOAT")
    return path


def wav_to_mp3(wav_path: str, mp3_path: str) -> str:
    """Transcode a WAV to MP3 using the resolved ffmpeg binary."""
    ffmpeg = resolve_ffmpeg()
    cmd = [
        ffmpeg,
        "-nostdin",
        "-v", "error",
        "-y",
        "-i", wav_path,
        "-codec:a", "libmp3lame",
        "-qscale:a", "2",
        mp3_path,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace"))
    assert os.path.isfile(mp3_path)
    return mp3_path
