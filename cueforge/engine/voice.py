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
        out_lo: int = 0,
        out_mono: bool = False,
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
        # Routing metadata only: render() stays 100% stereo. The engine reads these
        # at mix time. out_lo is the 0-based first bus column; out_mono downmixes the
        # stereo block into that single column. Stored on the voice so a dying/
        # declicking voice keeps routing to its own destination.
        self.out_lo = max(0, int(out_lo))
        self.out_mono = bool(out_mono)
        self.gain = float(db_to_gain(gain_db))

        # Live gain ramp: the moving BASE level every other envelope multiplies
        # (fade_in/out, kill, pause). ``self.gain`` is the current settled/mid-ramp
        # amplitude; a ramp retargets from it so rapid re-issues compose smoothly.
        self._gain_start = self.gain      # amplitude at the current ramp's start
        self._gain_target = self.gain     # amplitude the ramp is heading to
        self._gain_ramp = 0               # frames left in the ramp (0 = settled)
        self._gain_ramp_total = 0         # total frames of the current ramp
        self._gain_shape = "linear"       # "linear" | "equalPower"
        self._gain_stop = False           # kill (declick) the voice when the ramp settles
        self._gain_stop_kill_frames = 1   # declick frames for the stop-when-done kill

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

        # Pause envelope: a separate declick ramp toward silence (pause) or back
        # to unity (resume). Once the ramp settles at zero the voice is fully
        # ``paused`` and emits silence WITHOUT advancing ``elapsed`` -- freezing
        # the engine clock freezes audio and pending waits as one thing.
        self.paused = False        # True once ramp-down completed: fully frozen
        self._pause_level = 1.0    # current pause-envelope multiplier (1 .. 0)
        self._pause_target = 1.0
        self._pause_ramp = 0       # frames left in the current ramp (0 = settled)

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

    def begin_gain_ramp(
        self,
        target_db: float,
        frames: int,
        *,
        shape: str = "linear",
        stop_when_done: bool = False,
        kill_frames: int = 1,
    ) -> None:
        """Ramp the base (live) gain toward ``target_db`` over ``frames`` frames.

        Retargets from ``self.gain`` (the current, possibly mid-ramp level) so a
        re-issue while a ramp is in flight composes without a click. When
        ``stop_when_done`` the voice declicks itself to silence on settle (a
        click-free drop for any target level).
        """
        frames = max(1, int(frames))
        self._gain_start = self.gain          # retarget from wherever we are now
        self._gain_target = float(db_to_gain(target_db))
        self._gain_ramp = frames
        self._gain_ramp_total = frames
        self._gain_shape = shape if shape in ("linear", "equalPower") else "linear"
        self._gain_stop = bool(stop_when_done)
        self._gain_stop_kill_frames = max(1, int(kill_frames))

    def _interp_factor(self, prog, start, target, shape):
        """Interpolation factor s in [0, 1] with s(0)=0, s(1)=1.

        The base level is ``start + (target - start) * s``. For ``equalPower`` the
        curve reuses the fade envelopes so a gain ramp matches a fade exactly: a
        rise tracks the equal-power fade-in, a descent the equal-power fade-out.
        """
        if shape == "equalPower":
            if target >= start:
                return fade_in_curve(prog, "equalPower")        # sin(prog*pi/2): rise like a fade-in
            return 1.0 - fade_out_curve(prog, "equalPower")     # 1-cos: descent matches a fade-out
        return prog

    def begin_pause(self, frames: int) -> None:
        """Ramp toward silence over ``frames`` frames, then freeze."""
        if self.paused:
            return
        self._pause_target = 0.0
        self._pause_ramp = max(1, int(frames))

    def begin_resume(self, frames: int) -> None:
        """Un-freeze and ramp back to unity over ``frames`` frames."""
        self.paused = False
        self._pause_target = 1.0
        self._pause_ramp = max(1, int(frames))

    # -- rendering ---------------------------------------------------------
    def render(self, n: int) -> np.ndarray:
        """Return this voice's enveloped contribution for ``n`` frames and
        advance its playhead. Shape (n, CHANNELS), float32."""
        n = int(n)
        total = self.total
        if self.finished or total == 0 or n <= 0:
            self.finished = True
            return np.zeros((max(n, 0), CHANNELS), dtype=NP_DTYPE)

        # Fully paused: emit silence and hold the playhead. A kill (PANIC/stop)
        # overrides the freeze so a paused voice can still be drained and dropped.
        if self.paused and not self.killing:
            return np.zeros((n, CHANNELS), dtype=NP_DTYPE)

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

        # Envelope (float64 for headroom, cast to float32 at the end). The live
        # gain ramp is the BASE level the fade/kill/pause envelopes multiply.
        if self._gain_ramp > 0:
            gtotal = self._gain_ramp_total    # NB: keep ``total`` (= self.total) intact
            done = gtotal - self._gain_ramp   # frames already ramped before this block
            prog = np.clip((done + offsets + 1) / float(gtotal), 0.0, 1.0)   # per-sample progress
            s = self._interp_factor(prog, self._gain_start, self._gain_target, self._gain_shape)
            env = (self._gain_start + (self._gain_target - self._gain_start) * s).astype(np.float64)
            advanced = min(n, self._gain_ramp)
            end_prog = min(1.0, (done + advanced) / float(gtotal))
            end_s = self._interp_factor(end_prog, self._gain_start, self._gain_target, self._gain_shape)
            self.gain = float(self._gain_start + (self._gain_target - self._gain_start) * end_s)
            self._gain_ramp = max(0, self._gain_ramp - n)
            if self._gain_ramp == 0:
                self.gain = self._gain_target
                if self._gain_stop:
                    self.begin_kill(self._gain_stop_kill_frames)   # click-free drop
        else:
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

        # Pause/resume declick, laid on top of the normal envelope. While the
        # ramp runs the voice keeps advancing (the declick is audible); the
        # freeze itself takes effect only once the ramp has settled at zero.
        if self._pause_ramp > 0:
            remaining = self._pause_ramp
            level = self._pause_level
            target = self._pause_target
            mult = level + (target - level) * (offsets + 1) / float(remaining)
            env *= np.clip(mult, 0.0, 1.0)
            advanced = min(n, remaining)
            self._pause_level = float(
                np.clip(level + (target - level) * advanced / float(remaining), 0.0, 1.0)
            )
            self._pause_ramp = max(0, remaining - n)
            if self._pause_ramp == 0 and self._pause_target <= 0.0:
                self.paused = True
                self._pause_level = 0.0
        elif self._pause_level < 1.0:
            # Settled below unity while a kill is draining a paused voice.
            env *= self._pause_level

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
