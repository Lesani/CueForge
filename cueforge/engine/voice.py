# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""A single playing PCM buffer with its envelope state.

A ``Voice`` owns one decoded PCM buffer plus everything needed to render a block
of output for it: static gain, an optional fade-in, an optional fade-out applied
before the natural end, seamless looping, and a "kill" ramp used for declick
cuts, stop fades and panic. The mixer sums the block returned by :meth:`render`
from every active voice.

All timing is tracked with a single monotonic ``elapsed`` frame counter. For a
looping voice the read index wraps (``elapsed % total``) while ``elapsed`` keeps
counting, so the fade-in fires only once at the initial start and never at a loop
boundary.
"""

from __future__ import annotations

import numpy as np

from cueforge.audio_format import (
    CHANNELS,
    NP_DTYPE,
    db_to_gain,
    seconds_to_frames,
)
from cueforge.engine.envelopes import fade_in_curve, fade_out_curve


class Voice:
    """One playing buffer. Not thread-safe on its own; the engine guards it."""

    def __init__(
        self,
        pcm: np.ndarray,
        *,
        cue_id: str | None = None,
        gain_db: float = 0.0,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
        fade_shape: str = "linear",
        loop: bool = False,
    ) -> None:
        pcm = np.ascontiguousarray(pcm, dtype=NP_DTYPE)
        if pcm.ndim == 1:
            # Up-mix mono to stereo.
            pcm = np.repeat(pcm[:, None], CHANNELS, axis=1)
        if pcm.ndim != 2 or pcm.shape[1] != CHANNELS:
            raise ValueError(f"pcm must have shape (frames, {CHANNELS})")

        self.pcm = pcm
        self.total = int(pcm.shape[0])
        self.cue_id = cue_id
        self.gain = float(db_to_gain(gain_db))
        self.fade_in_frames = max(0, seconds_to_frames(fade_in))
        self.fade_out_frames = max(0, seconds_to_frames(fade_out))
        self.fade_shape = fade_shape if fade_shape in ("linear", "equalPower") else "linear"
        self.loop = bool(loop)

        self.elapsed = 0          # monotonic frames rendered since start
        self.finished = False

        # Kill ramp: a linear fade-to-zero laid on top of the normal envelope,
        # used for declick cuts, stop-fades and panic. When it reaches zero the
        # voice is finished and the engine drops it.
        self.killing = False
        self.kill_total = 0
        self.kill_left = 0

    # -- control -----------------------------------------------------------
    def begin_kill(self, frames: int) -> None:
        """Start (or shorten) a linear fade-to-zero over ``frames`` frames."""
        frames = max(1, int(frames))
        if self.killing:
            # Never let a new request make an in-progress kill slower.
            self.kill_left = min(self.kill_left, frames)
            self.kill_total = max(1, self.kill_left)
            return
        self.killing = True
        self.kill_total = frames
        self.kill_left = frames

    # -- rendering ---------------------------------------------------------
    def render(self, n: int) -> np.ndarray:
        """Return this voice's enveloped contribution for ``n`` frames and
        advance its playhead. Shape (n, CHANNELS), float32."""
        n = int(n)
        total = self.total
        if self.finished or total == 0 or n <= 0:
            self.finished = True
            return np.zeros((max(n, 0), CHANNELS), dtype=NP_DTYPE)

        offsets = np.arange(n)
        frames = self.elapsed + offsets

        # Source samples for this block.
        if self.loop:
            block = self.pcm[frames % total]
        else:
            block = np.zeros((n, CHANNELS), dtype=NP_DTYPE)
            start = self.elapsed
            if start < total:
                k = min(n, total - start)
                block[:k] = self.pcm[start:start + k]

        # Envelope (float64 for headroom, cast to float32 at the end).
        env = np.full(n, self.gain, dtype=np.float64)

        if self.fade_in_frames > 0:
            env *= fade_in_curve(frames / float(self.fade_in_frames), self.fade_shape)

        if (not self.loop) and self.fade_out_frames > 0:
            fo_start = total - self.fade_out_frames
            p = (frames - fo_start) / float(self.fade_out_frames)
            env *= fade_out_curve(p, self.fade_shape)

        if self.killing:
            kf = (self.kill_left - offsets) / float(self.kill_total)
            env *= np.clip(kf, 0.0, 1.0)
            self.kill_left -= n

        out = (block * env[:, None]).astype(NP_DTYPE)
        self.elapsed += n

        if self.killing and self.kill_left <= 0:
            self.finished = True
        if (not self.loop) and self.elapsed >= total:
            self.finished = True
        return out

    # -- introspection -----------------------------------------------------
    @property
    def playhead(self) -> int:
        """Current playhead in frames. For a loop, position within the loop."""
        if self.loop and self.total > 0:
            return int(self.elapsed % self.total)
        return int(min(self.elapsed, self.total))
