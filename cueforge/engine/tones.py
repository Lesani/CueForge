# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Generated identification tones for the Output Test button."""
from __future__ import annotations

import numpy as np

from cueforge.audio_format import NP_DTYPE, SAMPLE_RATE


def make_identification_tone(mono: bool) -> np.ndarray:
    """Return a stereo (n, 2) float32 identification tone.

    Stereo output: a beep in L, a gap, then a beep in R (so the operator hears
    left-then-right and can place the speaker). Mono output: a single centered
    beep (L == R); the engine mean-downmixes it into the one channel.
    """
    beep = int(0.25 * SAMPLE_RATE)
    gap = int(0.15 * SAMPLE_RATE)
    freq = 880.0
    t = np.arange(beep)
    beep_wave = 0.4 * np.sin(2.0 * np.pi * freq * t / SAMPLE_RATE)
    beep_wave *= np.minimum(1.0, np.minimum(t, beep - t) / (0.01 * SAMPLE_RATE))  # 10 ms declick ends
    beep_wave = beep_wave.astype(NP_DTYPE)
    if mono:
        return np.column_stack([beep_wave, beep_wave])
    silence_b = np.zeros(beep, dtype=NP_DTYPE)
    silence_g = np.zeros(gap, dtype=NP_DTYPE)
    left = np.concatenate([beep_wave, silence_g, silence_b])
    right = np.concatenate([silence_b, silence_g, beep_wave])
    return np.ascontiguousarray(np.column_stack([left, right]), dtype=NP_DTYPE)
