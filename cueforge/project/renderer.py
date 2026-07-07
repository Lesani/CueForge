# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Offline flatten of a compound cue timeline into one stereo FLAC blob.

Pure numpy at the engine format. Mirrors the engine's fade-envelope math
(cueforge.engine.envelopes) and final soft-limiter so an offline render sounds
like the live mixer would. Content-addressed by the rendered PCM, written via
the same hardened .part write as the importer.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Callable, Optional

import numpy as np
import soundfile as sf

from ..audio_format import CHANNELS, NP_DTYPE, SAMPLE_RATE, db_to_gain, seconds_to_frames
from ..engine.envelopes import fade_in_curve, fade_out_curve
from ..engine.audio_engine import _soft_limit
from .render import write_flac_atomic

# Cap total render length. 15 min stereo f32 accumulator ~= 330 MB; compounds
# are short in practice. A timeline longer than this is clamped (clips past the
# cap are truncated). ASCII-only comments/messages (cp1252 console).
MAX_RENDER_SECONDS = 15 * 60
MAX_RENDER_FRAMES = MAX_RENDER_SECONDS * SAMPLE_RATE

# Signature format tag: bump when the renderer's audio result changes so old
# render_signatures are treated as dirty and re-rendered.
_SIG_VERSION = "v1"


class RenderError(Exception):
    """Raised when a timeline cannot be rendered (e.g. no audible content)."""


def compound_signature(timeline: dict, resolve_hash: Callable[[Optional[str]], Optional[str]]) -> str:
    """Stable hash over the audio-affecting fields of a timeline plus each
    clip source's current audio hash. Excludes cosmetic ids/names."""
    tracks_sig = []
    for tr in (timeline or {}).get("tracks", []) or []:
        clips_sig = []
        for cl in tr.get("clips", []) or []:
            sid = cl.get("itemId")
            co = cl.get("clipOut")
            clips_sig.append([
                sid,
                resolve_hash(sid),                      # source audio hash or None
                round(float(cl.get("start", 0) or 0), 6),
                round(float(cl.get("clipIn", 0) or 0), 6),
                None if co is None else round(float(co), 6),
                round(float(cl.get("gainDb", 0) or 0), 6),
                round(float(cl.get("fadeIn", 0) or 0), 6),
                round(float(cl.get("fadeOut", 0) or 0), 6),
                str(cl.get("fadeShape", "linear")),
            ])
        tracks_sig.append([
            round(float(tr.get("gainDb", 0) or 0), 6),
            bool(tr.get("mute", False)),
            clips_sig,
        ])
    payload = json.dumps([_SIG_VERSION, tracks_sig], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def render_timeline(timeline: dict,
                    path_map: dict,          # source item id -> flac path or None
                    audio_dir: str) -> tuple[str, float]:
    """Flatten ``timeline`` to one stereo blob; write FLAC into ``audio_dir``.

    Returns (content_hash, duration_seconds). Raises RenderError when the
    timeline yields no audible frames (empty / all sources missing).
    """
    decoded: dict = {}   # flac path -> (n,2) float32, decode each source once

    def _load(path):
        if path not in decoded:
            data, _sr = sf.read(path, dtype="float32", always_2d=True)
            if data.shape[1] == 1:
                data = np.repeat(data, 2, axis=1)   # defensive; stored audio is stereo
            decoded[path] = np.ascontiguousarray(data[:, :CHANNELS], dtype=NP_DTYPE)
        return decoded[path]

    # Pass 1: build per-clip (start_frame, seg_after_env); track max end.
    jobs = []
    total_frames = 0
    for tr in (timeline or {}).get("tracks", []) or []:
        if tr.get("mute", False):
            continue
        track_gain = db_to_gain(float(tr.get("gainDb", 0) or 0))
        for cl in tr.get("clips", []) or []:
            path = path_map.get(cl.get("itemId"))
            if not path:
                continue                      # deleted / missing source -> silent
            src = _load(path)
            n = src.shape[0]
            ci = seconds_to_frames(float(cl.get("clipIn", 0) or 0))
            ci = max(0, min(ci, n))
            co_val = cl.get("clipOut")
            co = n if co_val is None else seconds_to_frames(float(co_val))
            co = max(ci, min(co, n))
            seg_len = co - ci
            if seg_len <= 0:
                continue                      # zero-length clip -> nothing to add
            start_frame = max(0, seconds_to_frames(float(cl.get("start", 0) or 0)))
            if start_frame >= MAX_RENDER_FRAMES:
                continue

            seg = src[ci:co].astype(np.float64) * (track_gain
                  * db_to_gain(float(cl.get("gainDb", 0) or 0)))

            frames = np.arange(seg_len)       # 0-based within the clip segment
            shape = str(cl.get("fadeShape", "linear"))
            fi = seconds_to_frames(float(cl.get("fadeIn", 0) or 0))
            if fi > 0:
                seg *= fade_in_curve(frames / float(fi), shape)[:, None]
            fo = seconds_to_frames(float(cl.get("fadeOut", 0) or 0))
            if fo > 0:
                fo_start = seg_len - fo
                p = (frames - fo_start) / float(fo)
                seg *= fade_out_curve(p, shape)[:, None]

            jobs.append((start_frame, seg))
            total_frames = max(total_frames, start_frame + seg_len)

    total_frames = min(total_frames, MAX_RENDER_FRAMES)
    if total_frames <= 0:
        raise RenderError("timeline has no audible content")

    master = np.zeros((total_frames, CHANNELS), dtype=np.float64)
    for start_frame, seg in jobs:
        s = start_frame
        e = s + seg.shape[0]
        if s >= total_frames:
            continue
        if e > total_frames:                  # clip runs past the cap: truncate
            seg = seg[:total_frames - s]
            e = total_frames
        master[s:e] += seg

    out = _soft_limit(master).astype(NP_DTYPE)
    out = np.ascontiguousarray(out, dtype=NP_DTYPE)
    audio_hash = hashlib.sha256(out.tobytes()).hexdigest()
    write_flac_atomic(os.path.join(audio_dir, f"{audio_hash}.flac"), out)
    return audio_hash, out.shape[0] / SAMPLE_RATE
