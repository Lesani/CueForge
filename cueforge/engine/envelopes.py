# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Fade-shape curves shared by the mixer voices.

A fade shape maps a normalized progress ``t`` in [0, 1] to a linear amplitude
multiplier. Two shapes are supported:

- ``"linear"``      -- constant-slope amplitude ramp.
- ``"equalPower"``  -- sin/cos (sqrt-power) ramp; at the midpoint the gain is
  ~0.707 rather than 0.5, keeping perceived loudness constant across a crossfade.

Both helpers accept scalars or numpy arrays and always return float64 so callers
can multiply them into an envelope without precision surprises.
"""

from __future__ import annotations

import numpy as np

_HALF_PI = np.pi / 2.0


def fade_in_curve(t, shape: str = "linear"):
    """Rising amplitude curve. t in [0, 1] -> gain in [0, 1] (0 at start)."""
    t = np.clip(np.asarray(t, dtype=np.float64), 0.0, 1.0)
    if shape == "equalPower":
        return np.sin(t * _HALF_PI)
    return t


def fade_out_curve(p, shape: str = "linear"):
    """Falling amplitude curve. p = progress in [0, 1] across the fade region,
    returns gain descending from 1 (p=0) to 0 (p=1).

    Note ``fade_out_curve(p) == fade_in_curve(1 - p)`` for both shapes, so an
    equal-power fade-out is the mirror image of an equal-power fade-in.
    """
    p = np.clip(np.asarray(p, dtype=np.float64), 0.0, 1.0)
    if shape == "equalPower":
        return np.cos(p * _HALF_PI)
    return 1.0 - p
