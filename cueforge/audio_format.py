# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Engine audio format constants and shared conversion helpers.

The whole system mixes at a single fixed format so the real-time mixer never has
to resample. All decoded audio is resampled to this format at import time.
"""

from __future__ import annotations

import numpy as np

# Fixed engine format. Everything the mixer touches is in this format.
SAMPLE_RATE = 48_000          # Hz
# CHANNELS is the STEREO SOURCE/VOICE width -- every voice renders (n, 2) and all
# stored audio is stereo. It is NOT the mix-bus width: multichannel output widens
# the engine's accumulator to the device's channel count (see AudioEngine
# ``_bus_channels``), into which each stereo voice is scattered at its output pair.
CHANNELS = 2                  # stereo
NP_DTYPE = np.float32         # internal PCM sample type

# PCM convention: numpy array of shape (num_frames, CHANNELS), dtype float32,
# nominal sample range [-1.0, 1.0].


def seconds_to_frames(seconds: float) -> int:
    """Convert a duration in seconds to a whole number of frames."""
    return int(round(seconds * SAMPLE_RATE))


def frames_to_seconds(frames: int) -> float:
    """Convert a frame count to seconds."""
    return frames / SAMPLE_RATE


def db_to_gain(db: float) -> float:
    """Convert decibels to a linear amplitude multiplier. 0 dB -> 1.0."""
    return float(10.0 ** (db / 20.0))


def gain_to_db(gain: float) -> float:
    """Convert a linear amplitude multiplier to decibels. Guards against <= 0."""
    if gain <= 0.0:
        return float("-inf")
    return float(20.0 * np.log10(gain))


def silence(num_frames: int) -> np.ndarray:
    """Return a silent PCM buffer of the given length in the engine format."""
    return np.zeros((num_frames, CHANNELS), dtype=NP_DTYPE)
