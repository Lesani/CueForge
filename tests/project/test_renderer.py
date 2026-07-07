# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Tests for the offline compound-cue renderer and its signature."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
import soundfile as sf

from cueforge.audio_format import SAMPLE_RATE, db_to_gain, seconds_to_frames
from cueforge.project.renderer import (
    MAX_RENDER_FRAMES,
    MAX_RENDER_SECONDS,
    RenderError,
    compound_signature,
    render_timeline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write_src(audio_dir, name, seconds=1.0, freq=440.0, amp=0.5):
    """Write a stereo tone FLAC and return (path, ndarray (n,2) float32)."""
    n = int(round(seconds * SAMPLE_RATE))
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    wave = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    stereo = np.stack([wave, wave], axis=1)
    path = os.path.join(audio_dir, name)
    sf.write(path, stereo, SAMPLE_RATE, format="FLAC")
    data, _sr = sf.read(path, dtype="float32", always_2d=True)
    return path, np.ascontiguousarray(data, dtype=np.float32)


def _clip(item_id, **kw):
    c = {"itemId": item_id, "start": 0.0, "clipIn": 0.0, "clipOut": None,
         "gainDb": 0.0, "fadeIn": 0.0, "fadeOut": 0.0, "fadeShape": "linear"}
    c.update(kw)
    return c


def _timeline(*tracks):
    return {"tracks": list(tracks)}


def _track(clips, gainDb=0.0, mute=False, name="Track"):
    return {"id": "t", "name": name, "gainDb": gainDb, "mute": mute, "clips": clips}


def _read(audio_dir, audio_hash):
    data, _sr = sf.read(os.path.join(audio_dir, f"{audio_hash}.flac"),
                        dtype="float32", always_2d=True)
    return data


# ---------------------------------------------------------------------------
# Placement / trim / fades
# ---------------------------------------------------------------------------
def test_render_single_clip_offset_placement(tmp_path):
    audio_dir = str(tmp_path)
    path, src = _write_src(audio_dir, "a.flac", seconds=0.5)
    tl = _timeline(_track([_clip("s1", start=0.5)]))
    h, dur = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)

    off = seconds_to_frames(0.5)
    assert np.allclose(out[:off], 0.0, atol=1e-4)          # leading silence
    assert np.max(np.abs(out[off:])) > 0.1                 # content follows
    assert out.shape[0] == off + src.shape[0]
    assert dur == pytest.approx(out.shape[0] / SAMPLE_RATE)


def test_render_trim_clip_in_out(tmp_path):
    audio_dir = str(tmp_path)
    path, src = _write_src(audio_dir, "a.flac", seconds=1.0)
    tl = _timeline(_track([_clip("s1", clipIn=0.2, clipOut=0.6)]))
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)

    ci = seconds_to_frames(0.2)
    co = seconds_to_frames(0.6)
    assert out.shape[0] == co - ci
    assert np.allclose(out, src[ci:co], atol=2e-4)


def test_render_clipout_null_uses_source_end(tmp_path):
    audio_dir = str(tmp_path)
    path, src = _write_src(audio_dir, "a.flac", seconds=0.7)
    tl = _timeline(_track([_clip("s1", clipOut=None)]))
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)
    assert out.shape[0] == src.shape[0]
    assert np.allclose(out, src, atol=2e-4)


def test_render_clip_fade_in_linear(tmp_path):
    audio_dir = str(tmp_path)
    # Constant-amplitude source so the fade envelope is easy to read.
    n = int(0.4 * SAMPLE_RATE)
    const = np.ones((n, 2), dtype=np.float32) * 0.5
    path = os.path.join(audio_dir, "c.flac")
    sf.write(path, const, SAMPLE_RATE, format="FLAC")
    fade = 0.2
    tl = _timeline(_track([_clip("s1", fadeIn=fade)]))
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)

    ff = seconds_to_frames(fade)
    assert abs(out[0, 0]) < 0.02                            # ~0 at start
    mid = out[ff // 2, 0]
    assert mid == pytest.approx(0.25, abs=0.02)             # ~0.5 * 0.5
    # Monotonic ramp across the fade region.
    ramp = out[:ff, 0]
    assert np.all(np.diff(ramp) >= -1e-4)


def test_render_clip_fade_out_linear(tmp_path):
    audio_dir = str(tmp_path)
    n = int(0.4 * SAMPLE_RATE)
    const = np.ones((n, 2), dtype=np.float32) * 0.5
    path = os.path.join(audio_dir, "c.flac")
    sf.write(path, const, SAMPLE_RATE, format="FLAC")
    fade = 0.2
    tl = _timeline(_track([_clip("s1", fadeOut=fade)]))
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)

    fo = seconds_to_frames(fade)
    assert abs(out[-1, 0]) < 0.02                           # ~0 at end
    mid = out[n - fo // 2, 0]
    assert mid == pytest.approx(0.25, abs=0.02)
    tail = out[n - fo:, 0]
    assert np.all(np.diff(tail) <= 1e-4)                    # descending


# ---------------------------------------------------------------------------
# Gains / mute / overlap
# ---------------------------------------------------------------------------
def test_render_track_gain_and_clip_gain(tmp_path):
    audio_dir = str(tmp_path)
    path, src = _write_src(audio_dir, "a.flac", seconds=0.3, amp=0.2)
    tl = _timeline(_track([_clip("s1", gainDb=-6.0)], gainDb=-3.0))
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)
    factor = db_to_gain(-3.0) * db_to_gain(-6.0)
    assert np.allclose(out, src * factor, atol=2e-4)


def test_render_track_mute_silences(tmp_path):
    audio_dir = str(tmp_path)
    path, src = _write_src(audio_dir, "a.flac", seconds=0.3)
    path2, _ = _write_src(audio_dir, "b.flac", seconds=0.3, amp=0.3)
    tl = _timeline(
        _track([_clip("s1")], mute=True),
        _track([_clip("s2")]),
    )
    h, _ = render_timeline(tl, {"s1": path, "s2": path2}, audio_dir)
    out = _read(audio_dir, h)
    # Muted track contributes nothing; result equals the unmuted track alone.
    _, src2 = _write_src(audio_dir, "b.flac", seconds=0.3, amp=0.3)
    assert np.allclose(out, src2, atol=2e-4)


def test_render_overlapping_clips_sum(tmp_path):
    audio_dir = str(tmp_path)
    path, src = _write_src(audio_dir, "a.flac", seconds=0.3, amp=0.2)
    tl = _timeline(
        _track([_clip("s1", start=0.0)]),
        _track([_clip("s1", start=0.0)]),
    )
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)
    assert np.allclose(out, src * 2.0, atol=2e-4)


def test_render_missing_source_renders_silent(tmp_path):
    audio_dir = str(tmp_path)
    path, src = _write_src(audio_dir, "a.flac", seconds=0.3)
    tl = _timeline(
        _track([_clip("missing", start=0.0)]),
        _track([_clip("s1", start=0.0)]),
    )
    h, _ = render_timeline(tl, {"missing": None, "s1": path}, audio_dir)
    out = _read(audio_dir, h)
    assert np.allclose(out, src, atol=2e-4)


# ---------------------------------------------------------------------------
# Empty / all-missing / caps / limiter
# ---------------------------------------------------------------------------
def test_render_empty_timeline_raises(tmp_path):
    with pytest.raises(RenderError):
        render_timeline(_timeline(), {}, str(tmp_path))


def test_render_all_missing_raises(tmp_path):
    tl = _timeline(_track([_clip("x"), _clip("y")]))
    with pytest.raises(RenderError):
        render_timeline(tl, {"x": None, "y": None}, str(tmp_path))


def test_render_caps_total_length(tmp_path):
    audio_dir = str(tmp_path)
    path, _ = _write_src(audio_dir, "a.flac", seconds=0.2)
    # start far beyond the cap -> clip omitted; a second in-range clip remains.
    tl = _timeline(_track([
        _clip("s1", start=MAX_RENDER_SECONDS + 10.0),
        _clip("s1", start=0.0),
    ]))
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)
    assert out.shape[0] <= MAX_RENDER_FRAMES


def test_render_limiter_applied(tmp_path):
    audio_dir = str(tmp_path)
    path, _ = _write_src(audio_dir, "a.flac", seconds=0.2, amp=0.9)
    clips = [_clip("s1", start=0.0) for _ in range(8)]
    tl = _timeline(_track(clips))
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out = _read(audio_dir, h)
    assert np.isfinite(out).all()
    assert np.max(np.abs(out)) < 2.0


# ---------------------------------------------------------------------------
# Dedup / file layout
# ---------------------------------------------------------------------------
def test_render_dedup_same_timeline_same_hash(tmp_path):
    audio_dir = str(tmp_path)
    path, _ = _write_src(audio_dir, "a.flac", seconds=0.3)
    tl = _timeline(_track([_clip("s1", start=0.1)]))
    h1, _ = render_timeline(tl, {"s1": path}, audio_dir)
    h2, _ = render_timeline(tl, {"s1": path}, audio_dir)
    assert h1 == h2
    flacs = [f for f in os.listdir(audio_dir) if f == f"{h1}.flac"]
    assert len(flacs) == 1


def test_render_writes_flac_at_hash_path(tmp_path):
    audio_dir = str(tmp_path)
    path, _ = _write_src(audio_dir, "a.flac", seconds=0.3)
    tl = _timeline(_track([_clip("s1")]))
    h, _ = render_timeline(tl, {"s1": path}, audio_dir)
    out_path = os.path.join(audio_dir, f"{h}.flac")
    assert os.path.isfile(out_path)
    data, _sr = sf.read(out_path, dtype="float32", always_2d=True)
    assert data.shape[1] == 2


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------
def _resolver(mapping):
    return lambda sid: mapping.get(sid)


def test_signature_stable_across_reserialization():
    tl = _timeline(_track([_clip("s1", start=0.25, gainDb=-3.0)]))
    res = _resolver({"s1": "hashA"})
    sig1 = compound_signature(tl, res)
    tl2 = json.loads(json.dumps(tl))
    sig2 = compound_signature(tl2, res)
    assert sig1 == sig2


def test_signature_changes_on_param_change():
    res = _resolver({"s1": "hashA"})
    tl1 = _timeline(_track([_clip("s1", gainDb=0.0)]))
    tl2 = _timeline(_track([_clip("s1", gainDb=-1.0)]))
    assert compound_signature(tl1, res) != compound_signature(tl2, res)


def test_signature_changes_on_source_hash_change():
    tl = _timeline(_track([_clip("s1")]))
    sig1 = compound_signature(tl, _resolver({"s1": "hashA"}))
    sig2 = compound_signature(tl, _resolver({"s1": "hashB"}))
    assert sig1 != sig2


def test_signature_ignores_ids_and_names():
    res = _resolver({"s1": "hashA"})
    tl1 = {"tracks": [{"id": "t1", "name": "One",
                       "gainDb": 0.0, "mute": False,
                       "clips": [dict(_clip("s1"), id="c1")]}]}
    tl2 = {"tracks": [{"id": "TRACK-XYZ", "name": "Renamed",
                       "gainDb": 0.0, "mute": False,
                       "clips": [dict(_clip("s1"), id="c-different")]}]}
    assert compound_signature(tl1, res) == compound_signature(tl2, res)
