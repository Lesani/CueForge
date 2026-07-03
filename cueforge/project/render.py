# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Turn a library item into engine-ready PCM plus engine parameters.

``load_cue_pcm`` applies TRIM only (non-destructive). Gain, fades, and loop are
engine parameters returned by ``cue_engine_params`` for the caller to hand to
the audio engine.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from ..audio_format import (
    NP_DTYPE,
    SAMPLE_RATE,
    gain_to_db,
    seconds_to_frames,
)
from .model import LibraryItem
from .storage import ProjectSession


def load_cue_pcm(session: ProjectSession, item: LibraryItem) -> np.ndarray:
    """Read the stored FLAC for ``item`` and apply trim, returning (n, 2) f32."""
    if item.audio_hash is None:
        raise ValueError(f"library item {item.id!r} has no audio (type {item.type})")
    path = session.audio_path(item.audio_hash)
    data, _sr = sf.read(path, dtype="float32", always_2d=True)
    pcm = np.ascontiguousarray(data, dtype=NP_DTYPE)

    n = pcm.shape[0]
    start = seconds_to_frames(item.trim_in) if item.trim_in else 0
    start = max(0, min(start, n))
    if item.trim_out is None:
        end = n
    else:
        end = seconds_to_frames(item.trim_out)
    end = max(start, min(end, n))
    return np.ascontiguousarray(pcm[start:end], dtype=NP_DTYPE)


def cue_engine_params(item: LibraryItem) -> dict:
    """Return the engine-facing parameters for a cue (not applied to PCM)."""
    return {
        "gain_db": item.gain_db,
        "fade_in": item.fade_in,
        "fade_out": item.fade_out,
        "fade_shape": item.fade_shape,
        "loop": item.loop,
    }


def normalize(
    session: ProjectSession,
    item: LibraryItem,
    target_dbfs: float = -1.0,
) -> None:
    """Set ``item.gain_db`` so the trimmed audio's peak hits ``target_dbfs``."""
    pcm = load_cue_pcm(session, item)
    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
    if peak <= 0.0:
        # Silent audio: no meaningful gain change.
        item.gain_db = 0.0
    else:
        peak_dbfs = gain_to_db(peak)
        item.gain_db = float(target_dbfs - peak_dbfs)
    session.autosave()
