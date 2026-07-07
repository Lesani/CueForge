# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Real-time software mixer for CueForge.

``AudioEngine`` is the single source of truth for mixing. The same internal
routine (:meth:`render`) feeds both offline rendering (used by the headless test
suite) and the live sounddevice callback, so what the tests verify is exactly
what the booth hears.

Channels
--------
- **normal**      -- exactly one normal cue at a time; a new one hard-cuts the
  current one with a ~10 ms declick ramp.
- **backgrounds** -- independent, stackable layers keyed by ``cue_id``; re-firing
  the same key restarts that one instance, different keys overlay.
- **audition**    -- a preview channel that never disturbs the show voices.

Thread-safety
-------------
Control methods may be called from any thread (WebSocket handlers, timers). They
do not touch shared state directly: each enqueues a command onto a lock-guarded
queue that is drained atomically at the start of every mix block (and before any
status read). This keeps the audio callback free of contention and guarantees
block-consistent state transitions.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

import numpy as np

from cueforge.audio_format import (
    CHANNELS,
    NP_DTYPE,
    SAMPLE_RATE,
    db_to_gain,
    seconds_to_frames,
)
from cueforge.engine.voice import Voice

# Ramp lengths.
DECLICK_SECONDS = 0.010   # ~10 ms hard-cut declick
PANIC_SECONDS = 0.150     # ~150 ms panic fade

# Simultaneous voice cap (~32 simultaneous voices).
MAX_BACKGROUNDS = 32

# Scheduled-fade target sentinel: resolve "all backgrounds" at activation time.
ALL_BACKGROUNDS = "__all_backgrounds__"


class _ScheduledFire:
    """A pending chain fire: apply ``activate`` once ``remaining`` frames elapse.

    ``activate`` is the exact same zero-arg closure a live command would run, so a
    scheduled fire and a live fire take an identical code path (see the ``_cmd_*``
    builders). ``kind`` is informational for the status snapshot only.
    """

    __slots__ = ("cue_id", "remaining", "kind", "activate")

    def __init__(self, cue_id, remaining, kind, activate) -> None:
        self.cue_id = cue_id
        self.remaining = int(remaining)
        self.kind = kind
        self.activate = activate


def _soft_limit(x: np.ndarray) -> np.ndarray:
    """Transparent below unity, soft-knee above it.

    Output equals the input exactly for |x| <= 1 (so a -6.02 dB tone stays at
    amplitude 0.5), and for |x| > 1 the excess is compressed through tanh so the
    sum of many voices can never overflow (|out| < 2). Continuous in value and
    slope at |x| = 1.
    """
    # A NaN/inf sample (corrupt decode) would sail through the knee below
    # (NaN comparisons are False) straight into the DAC; silence it instead.
    if not np.isfinite(x).all():
        x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    ax = np.abs(x)
    mask = ax > 1.0
    if not mask.any():
        return x
    y = np.array(x, copy=True)
    y[mask] = np.sign(x[mask]) * (1.0 + np.tanh(ax[mask] - 1.0))
    return y


class AudioEngine:
    """Software mixer with normal / background / audition channels."""

    def __init__(self, *, bus_channels: int = CHANNELS) -> None:
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS

        # Mix-bus width: the render/accumulator width each voice is scattered
        # into (floor 2, cap 32). ``CHANNELS`` stays the stereo VOICE/source
        # width; the bus is wider only on a multichannel device.
        self._bus_channels = max(2, min(32, int(bus_channels)))
        # True only for a truly mono device: the bus stays 2-wide (so the full
        # mix is preserved) and the output stream is opened at 1 channel.
        self._mono_out = False

        self._declick_frames = max(1, seconds_to_frames(DECLICK_SECONDS))
        self._panic_frames = max(1, seconds_to_frames(PANIC_SECONDS))

        # Shared state -- only ever mutated while holding ``_lock`` (commands are
        # applied under it in ``_drain``).
        self._lock = threading.Lock()
        self._commands: deque = deque()
        self._normal: Voice | None = None
        self._backgrounds: "OrderedDict[str, Voice]" = OrderedDict()
        self._audition: Voice | None = None
        self._dying: list[Voice] = []   # voices fading out (declick/stop/panic)
        # Pending chain fires, counted down sample-accurately in render(). See
        # docs/adr/0001-chain-timing-on-the-engine-clock.md.
        self._scheduled: list[_ScheduledFire] = []
        self._paused = False            # global pause freezes clock + waits

        # Master trim: a device-level gain applied to the summed mix before the
        # soft limiter, smoothed over ~50 ms so a settings change never steps.
        self._master_gain = 1.0
        self._master_start = 1.0
        self._master_target = 1.0
        self._master_ramp = 0
        self._master_smooth_frames = max(1, seconds_to_frames(0.050))

        # Output / device handling.
        self.device_ok = True
        self._stream = None
        self._device = None
        self._stop_requested = False
        self._reconnect_thread: threading.Thread | None = None
        self._reconnect_stop = threading.Event()

        # Output-format adaptation. The mixer always renders at the engine format
        # (SAMPLE_RATE, CHANNELS). The chosen device may run at a different native
        # rate/channel count, so the output stream is opened at the DEVICE's native
        # format and each callback block is resampled/remapped into it. Opening at
        # a fixed 48 kHz on a device whose path does not transparently resample
        # (WASAPI-exclusive, WDM-KS, or a mismatched endpoint) clocks the samples
        # out at the wrong rate -> garbled, wrong-speed playback.
        self._output_rate = SAMPLE_RATE
        self._output_channels = self._bus_channels
        self._resampler = None
        self._resample_buf = np.zeros((0, self._bus_channels), dtype=NP_DTYPE)

    # =====================================================================
    # Bus width
    # =====================================================================
    def set_bus_channels(self, n: int, *, mono_out: bool = False) -> None:
        """Set the mix-bus width (and mono-device flag) from a control thread.

        The width fields are mutated while holding ``_lock`` so the audio
        callback's ``render()`` can never observe a half-updated bus. Safe to
        call directly from control threads: ``render()`` releases the lock
        between blocks. Do NOT call it from within a command closure.
        """
        with self._lock:
            self._mono_out = bool(mono_out)
            self._bus_channels = 2 if mono_out else max(2, min(32, int(n)))
            self._output_channels = 1 if mono_out else self._bus_channels
            self._resample_buf = np.zeros((0, self._bus_channels), dtype=NP_DTYPE)

    # =====================================================================
    # Command queue plumbing
    # =====================================================================
    def _enqueue(self, fn) -> None:
        with self._lock:
            self._commands.append(fn)

    def _drain_locked(self) -> None:
        """Apply all queued commands. Caller must hold ``_lock``."""
        while self._commands:
            self._commands.popleft()()

    # =====================================================================
    # Command builders -- one mutation, shared by the live and scheduled paths
    # =====================================================================
    # Each ``_cmd_*`` returns a zero-arg closure that applies the state change.
    # Live methods enqueue it immediately; a scheduled fire runs the identical
    # closure when its countdown reaches zero, so a delayed chain member and a
    # live tap take a byte-for-byte identical path.
    def _cmd_play_normal(self, voice: Voice):
        def cmd() -> None:
            if self._normal is not None:
                self._normal.begin_kill(self._declick_frames)
                self._dying.append(self._normal)
            self._normal = voice

        return cmd

    def _cmd_stop_normal(self):
        def cmd() -> None:
            if self._normal is not None:
                self._normal.begin_kill(self._declick_frames)
                self._dying.append(self._normal)
                self._normal = None

        return cmd

    def _cmd_play_background(self, cue_id: str, voice: Voice):
        def cmd() -> None:
            existing = self._backgrounds.pop(cue_id, None)
            if existing is not None:
                # Declick the old instance out so the restart doesn't pop; the
                # dict still holds exactly one voice for this cue_id.
                existing.begin_kill(self._declick_frames)
                self._dying.append(existing)
            elif len(self._backgrounds) >= MAX_BACKGROUNDS:
                # Evict the oldest running background to honour the voice cap.
                _, oldest = self._backgrounds.popitem(last=False)
                oldest.begin_kill(self._declick_frames)
                self._dying.append(oldest)
            self._backgrounds[cue_id] = voice

        return cmd

    def _cmd_stop_background(self, cue_id: str, mode: str, fade_seconds: float):
        def cmd() -> None:
            voice = self._backgrounds.pop(cue_id, None)
            if voice is None:
                return  # safe no-op if the target is not live (see schedule_*)
            if mode == "hard":
                return  # immediate: simply dropped, not rendered again
            voice.begin_kill(max(1, seconds_to_frames(fade_seconds)))
            self._dying.append(voice)

        return cmd

    def _cmd_stop_all_backgrounds(self, mode: str, fade_seconds: float):
        def cmd() -> None:
            voices = list(self._backgrounds.values())
            self._backgrounds.clear()
            if mode == "hard":
                return
            frames = max(1, seconds_to_frames(fade_seconds))
            for voice in voices:
                voice.begin_kill(frames)
                self._dying.append(voice)

        return cmd

    def _voices_for(self, cue_id: str) -> list[Voice]:
        """Live show voices whose engine key matches ``cue_id`` (normal, one
        background layer, and/or the audition voice keyed ``"__audition__"``)."""
        voices: list[Voice] = []
        if self._normal is not None and self._normal.cue_id == cue_id:
            voices.append(self._normal)
        bg = self._backgrounds.get(cue_id)
        if bg is not None:
            voices.append(bg)
        if self._audition is not None and self._audition.cue_id == cue_id:
            voices.append(self._audition)
        return voices

    def _cmd_set_cue_gain(self, cue_id, target_db, frames, shape, stop_when_done):
        def cmd() -> None:
            for v in self._voices_for(cue_id):
                v.begin_gain_ramp(target_db, frames, shape=shape, stop_when_done=stop_when_done,
                                  kill_frames=self._declick_frames)

        return cmd

    def _cmd_fade_all_backgrounds(self, target_db, frames, shape, stop_when_done):
        def cmd() -> None:
            for v in self._backgrounds.values():
                v.begin_gain_ramp(target_db, frames, shape=shape, stop_when_done=stop_when_done,
                                  kill_frames=self._declick_frames)

        return cmd

    # =====================================================================
    # Public control API (live)
    # =====================================================================
    def play_normal(
        self,
        cue_id: str,
        pcm: np.ndarray,
        *,
        gain_db: float = 0.0,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
        fade_shape: str = "linear",
        out_lo: int = 0,
        out_mono: bool = False,
    ) -> None:
        """Play ``pcm`` on the exclusive normal channel, hard-cutting whatever is
        currently on it with a ~10 ms declick ramp."""
        voice = Voice(
            pcm,
            cue_id=cue_id,
            gain_db=gain_db,
            fade_in=fade_in,
            fade_out=fade_out,
            fade_shape=fade_shape,
            loop=False,
            out_lo=out_lo,
            out_mono=out_mono,
        )
        self._enqueue(self._cmd_play_normal(voice))

    def stop_normal(self) -> None:
        """Declick-kill the live normal voice (a no-op if none). Used by the hub to
        enforce normal exclusivity across devices on a live fire."""
        self._enqueue(self._cmd_stop_normal())

    def schedule_stop_normal(self, cue_id: str, start_in_frames: int) -> None:
        """Schedule a normal-voice kill ``start_in_frames`` from now, keyed by
        ``cue_id`` (the firing placement's id, so it cancels with the chain). Used by
        the hub so a scheduled normal on another engine takes over exclusivity
        sample-accurately on this engine's own clock."""
        sf = _ScheduledFire(cue_id, int(start_in_frames), "stopNormal", self._cmd_stop_normal())
        self._enqueue(lambda: self._scheduled.append(sf))

    def play_background(
        self,
        cue_id: str,
        pcm: np.ndarray,
        *,
        gain_db: float = 0.0,
        fade_in: float = 0.0,
        loop: bool = False,
        fade_shape: str = "linear",
        out_lo: int = 0,
        out_mono: bool = False,
    ) -> None:
        """Start/restart a background layer keyed by ``cue_id``. Same key ->
        restart the single instance; different keys stack."""
        voice = Voice(
            pcm,
            cue_id=cue_id,
            gain_db=gain_db,
            fade_in=fade_in,
            fade_out=0.0,
            fade_shape=fade_shape,
            loop=loop,
            out_lo=out_lo,
            out_mono=out_mono,
        )
        self._enqueue(self._cmd_play_background(cue_id, voice))

    def stop_background(
        self,
        cue_id: str,
        *,
        mode: str = "fade",
        fade_seconds: float = 2.0,
    ) -> None:
        """Stop one background: ``mode="hard"`` removes it immediately,
        ``mode="fade"`` ramps it to silence over ``fade_seconds``."""
        self._enqueue(self._cmd_stop_background(cue_id, mode, fade_seconds))

    def stop_all_backgrounds(
        self,
        *,
        mode: str = "fade",
        fade_seconds: float = 2.0,
    ) -> None:
        """Stop every background (hard or fade)."""
        self._enqueue(self._cmd_stop_all_backgrounds(mode, fade_seconds))

    def set_cue_gain(
        self,
        cue_id: str,
        target_db: float,
        ramp_seconds: float,
        *,
        shape: str = "linear",
        stop_when_done: bool = False,
    ) -> None:
        """Ramp the live gain of the voice(s) keyed by ``cue_id`` to ``target_db``.

        A safe no-op if no voice matches (e.g. a live-edit for a placement that is
        not currently playing). ``stop_when_done`` drops the voice (declicked) when
        the ramp settles.
        """
        frames = max(1, seconds_to_frames(ramp_seconds))
        self._enqueue(self._cmd_set_cue_gain(cue_id, target_db, frames, shape, stop_when_done))

    def set_all_backgrounds_gain(
        self,
        target_db: float,
        ramp_seconds: float,
        *,
        shape: str = "linear",
        stop_when_done: bool = False,
    ) -> None:
        """Ramp every running background's live gain to ``target_db``."""
        frames = max(1, seconds_to_frames(ramp_seconds))
        self._enqueue(self._cmd_fade_all_backgrounds(target_db, frames, shape, stop_when_done))

    def set_master_gain(self, db: float) -> None:
        """Set the master device trim to ``db``, smoothed over ~50 ms."""

        def cmd() -> None:
            self._master_start = self._master_gain
            self._master_target = float(db_to_gain(db))
            self._master_ramp = self._master_smooth_frames

        self._enqueue(cmd)

    # =====================================================================
    # Scheduled control API (chain fires) -- each lands block-atomically
    # =====================================================================
    def schedule_normal(
        self,
        cue_id: str,
        pcm: np.ndarray,
        start_in_frames: int,
        *,
        gain_db: float = 0.0,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
        fade_shape: str = "linear",
        out_lo: int = 0,
        out_mono: bool = False,
    ) -> None:
        """Fire a normal cue ``start_in_frames`` frames from now."""
        voice = Voice(
            pcm,
            cue_id=cue_id,
            gain_db=gain_db,
            fade_in=fade_in,
            fade_out=fade_out,
            fade_shape=fade_shape,
            loop=False,
            out_lo=out_lo,
            out_mono=out_mono,
        )
        sf = _ScheduledFire(
            cue_id, start_in_frames, "normal", self._cmd_play_normal(voice)
        )
        self._enqueue(lambda: self._scheduled.append(sf))

    def schedule_background(
        self,
        cue_id: str,
        pcm: np.ndarray,
        start_in_frames: int,
        *,
        gain_db: float = 0.0,
        fade_in: float = 0.0,
        loop: bool = False,
        fade_shape: str = "linear",
        out_lo: int = 0,
        out_mono: bool = False,
    ) -> None:
        """Start a background cue ``start_in_frames`` frames from now."""
        voice = Voice(
            pcm,
            cue_id=cue_id,
            gain_db=gain_db,
            fade_in=fade_in,
            fade_out=0.0,
            fade_shape=fade_shape,
            loop=loop,
            out_lo=out_lo,
            out_mono=out_mono,
        )
        sf = _ScheduledFire(
            cue_id, start_in_frames, "background", self._cmd_play_background(cue_id, voice)
        )
        self._enqueue(lambda: self._scheduled.append(sf))

    def schedule_stop_all_backgrounds(
        self,
        cue_id: str,
        start_in_frames: int,
        *,
        mode: str = "fade",
        fade_seconds: float = 2.0,
    ) -> None:
        """Stop every background ``start_in_frames`` frames from now.

        ``cue_id`` is the stop placement's own id -- the schedule key for
        cancellation and the armed-cell display, not a stop target.
        """
        sf = _ScheduledFire(
            cue_id, start_in_frames, "stop", self._cmd_stop_all_backgrounds(mode, fade_seconds)
        )
        self._enqueue(lambda: self._scheduled.append(sf))

    def schedule_stop_background(
        self,
        cue_id: str,
        target_id: str,
        start_in_frames: int,
        *,
        mode: str = "fade",
        fade_seconds: float = 2.0,
    ) -> None:
        """Stop the ``target_id`` background ``start_in_frames`` frames from now.

        ``cue_id`` keys the pending fire (the stop placement's own id, for
        cancellation/armed display); ``target_id`` is the background to stop.
        A safe no-op if ``target_id`` is not a live background at activation.
        """
        sf = _ScheduledFire(
            cue_id, start_in_frames, "stop", self._cmd_stop_background(target_id, mode, fade_seconds)
        )
        self._enqueue(lambda: self._scheduled.append(sf))

    def schedule_fade(
        self,
        cue_id: str,
        target: str,
        start_in_frames: int,
        target_db: float,
        ramp_seconds: float,
        *,
        shape: str = "linear",
        stop_when_done: bool = False,
    ) -> None:
        """Ramp gain ``start_in_frames`` frames from now.

        ``cue_id`` is the fade placement's own id (the schedule key for
        cancellation and the armed-cell display). ``target`` is either the
        ``ALL_BACKGROUNDS`` sentinel (resolved to the live backgrounds AT
        activation) or a specific voice key (a safe no-op if not live then).
        """
        frames = max(1, seconds_to_frames(ramp_seconds))
        if target == ALL_BACKGROUNDS:
            activate = self._cmd_fade_all_backgrounds(target_db, frames, shape, stop_when_done)
        else:
            activate = self._cmd_set_cue_gain(target, target_db, frames, shape, stop_when_done)
        sf = _ScheduledFire(cue_id, start_in_frames, "fade", activate)
        self._enqueue(lambda: self._scheduled.append(sf))

    def cancel_scheduled(self, cue_id: str) -> None:
        """Drop every pending fire keyed by ``cue_id``."""

        def cmd() -> None:
            self._scheduled = [sf for sf in self._scheduled if sf.cue_id != cue_id]

        self._enqueue(cmd)

    def cancel_all_scheduled(self) -> None:
        """Drop every pending fire."""

        def cmd() -> None:
            self._scheduled = []

        self._enqueue(cmd)

    # =====================================================================
    # Global pause / resume
    # =====================================================================
    def pause_all(self) -> None:
        """Freeze all show voices (~10 ms declick) and freeze pending fires.

        Audition and already-dying voices are not touched. A cue fired while
        paused plays normally.
        """

        def cmd() -> None:
            self._paused = True
            if self._normal is not None:
                self._normal.begin_pause(self._declick_frames)
            for voice in self._backgrounds.values():
                voice.begin_pause(self._declick_frames)

        self._enqueue(cmd)

    def resume_all(self) -> None:
        """Un-freeze paused voices (~10 ms declick) and resume countdowns."""

        def cmd() -> None:
            self._paused = False
            if self._normal is not None:
                self._normal.begin_resume(self._declick_frames)
            for voice in self._backgrounds.values():
                voice.begin_resume(self._declick_frames)

        self._enqueue(cmd)

    def panic(self) -> None:
        """Fast-fade EVERYTHING over ~150 ms then clear all voices."""

        def cmd() -> None:
            frames = self._panic_frames
            if self._normal is not None:
                self._normal.begin_kill(frames)
                self._dying.append(self._normal)
                self._normal = None
            for voice in self._backgrounds.values():
                voice.begin_kill(frames)
                self._dying.append(voice)
            self._backgrounds.clear()
            if self._audition is not None:
                self._audition.begin_kill(frames)
                self._dying.append(self._audition)
                self._audition = None
            # Drop pending chain fires and lift any pause in the same command.
            self._scheduled.clear()
            self._paused = False

        self._enqueue(cmd)

    def audition(
        self,
        pcm: np.ndarray,
        *,
        gain_db: float = 0.0,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
        fade_shape: str = "linear",
        loop: bool = False,
        out_lo: int = 0,
        out_mono: bool = False,
    ) -> None:
        """Play ``pcm`` on the separate audition channel (replaces any current
        audition; never touches show voices)."""
        voice = Voice(
            pcm,
            cue_id="__audition__",
            gain_db=gain_db,
            fade_in=fade_in,
            fade_out=fade_out,
            fade_shape=fade_shape,
            loop=loop,
            out_lo=out_lo,
            out_mono=out_mono,
        )

        def cmd() -> None:
            if self._audition is not None:
                self._audition.begin_kill(self._declick_frames)
                self._dying.append(self._audition)
            self._audition = voice

        self._enqueue(cmd)

    def stop_audition(self) -> None:
        """Declick the audition channel to silence."""

        def cmd() -> None:
            if self._audition is not None:
                self._audition.begin_kill(self._declick_frames)
                self._dying.append(self._audition)
                self._audition = None

        self._enqueue(cmd)

    def stop_cue(self, cue_id: str) -> None:
        """Immediately stop the show voice(s) for ``cue_id`` with a declick.

        Used when a placement/column/page/library-item is deleted while its cue
        is playing so the audio stops with the DOM. Kills the normal voice if it
        matches, and/or a background layer keyed by ``cue_id``. No-op otherwise.
        """

        def cmd() -> None:
            if self._normal is not None and self._normal.cue_id == cue_id:
                self._normal.begin_kill(self._declick_frames)
                self._dying.append(self._normal)
                self._normal = None
            voice = self._backgrounds.pop(cue_id, None)
            if voice is not None:
                voice.begin_kill(self._declick_frames)
                self._dying.append(voice)
            # Also drop any pending fire for this cue (deletion mid-wait).
            self._scheduled = [sf for sf in self._scheduled if sf.cue_id != cue_id]

        self._enqueue(cmd)

    # =====================================================================
    # Mixing -- single source of truth
    # =====================================================================
    def render(self, num_frames: int) -> np.ndarray:
        """Advance the mix by ``num_frames`` and return float32
        (num_frames, _bus_channels)."""
        num_frames = int(num_frames)
        if num_frames <= 0:
            return np.zeros((0, self._bus_channels), dtype=NP_DTYPE)

        with self._lock:
            self._drain_locked()

            acc = np.zeros((num_frames, self._bus_channels), dtype=np.float64)

            if self._paused:
                # Frozen: scheduled countdowns and activations do not advance.
                # Voices render as usual -- paused ones emit silence and hold
                # their playhead; a voice started while paused plays normally.
                # Master trim still advances (it is the device trim; a settings
                # change while paused still lands).
                self._render_voices(acc, 0, num_frames)
                self._cleanup_finished()
                self._apply_master(acc, num_frames)
                return _soft_limit(acc).astype(NP_DTYPE)

            # Split the block at each scheduled fire that lands inside it so the
            # activation is sample-accurate. Render each segment, then fire at
            # its trailing boundary; the newly-started voice renders from there.
            boundaries = sorted(
                {sf.remaining for sf in self._scheduled if 0 <= sf.remaining < num_frames}
            )
            cursor = 0
            for b in boundaries + [num_frames]:
                if b > cursor:
                    self._render_voices(acc, cursor, b)
                    cursor = b
                if b < num_frames:
                    for sf in [s for s in self._scheduled if s.remaining == b]:
                        sf.activate()
                    self._scheduled = [s for s in self._scheduled if s.remaining != b]

            # Count down the fires that did not activate this block.
            for sf in self._scheduled:
                sf.remaining -= num_frames

            self._cleanup_finished()
            self._apply_master(acc, num_frames)

        return _soft_limit(acc).astype(NP_DTYPE)

    def _apply_master(self, acc: np.ndarray, n: int) -> np.ndarray:
        """Apply the smoothed master trim to the summed mix in place (pre-limit)."""
        if self._master_ramp > 0:
            total = self._master_smooth_frames
            done = total - self._master_ramp
            prog = np.clip((done + np.arange(n) + 1) / float(total), 0.0, 1.0)
            g = self._master_start + (self._master_target - self._master_start) * prog
            acc *= g[:, None]
            advanced = min(n, self._master_ramp)
            self._master_gain = float(self._master_start + (self._master_target - self._master_start)
                                      * min(1.0, (done + advanced) / float(total)))
            self._master_ramp = max(0, self._master_ramp - n)
            if self._master_ramp == 0:
                self._master_gain = self._master_target
        elif self._master_gain != 1.0:
            acc *= self._master_gain
        return acc

    def _render_voices(self, acc: np.ndarray, start: int, end: int) -> None:
        """Render every active voice for the segment ``[start:end)`` into ``acc``."""
        seg = end - start
        if seg <= 0:
            return
        if self._normal is not None:
            self._mix_voice(acc, start, end, self._normal.render(seg),
                            self._normal.out_lo, self._normal.out_mono)
        for voice in self._backgrounds.values():
            self._mix_voice(acc, start, end, voice.render(seg), voice.out_lo, voice.out_mono)
        if self._audition is not None:
            self._mix_voice(acc, start, end, self._audition.render(seg),
                            self._audition.out_lo, self._audition.out_mono)
        for voice in self._dying:
            self._mix_voice(acc, start, end, voice.render(seg), voice.out_lo, voice.out_mono)

    def _mix_voice(self, acc, start, end, block, out_lo, out_mono) -> None:
        """Scatter a voice's stereo ``block`` (seg, 2) into the wide bus.

        Device mono (``self._mono_out``): collapse everything into the 2-wide bus.
        Otherwise, a stereo voice lands in [out_lo:out_lo+2]; an ``out_mono`` voice
        is mean-downmixed into the single column [out_lo]. A destination beyond the
        bus width is dropped (silent), never an error.
        """
        if self._mono_out:
            acc[start:end, 0:2] += block          # device mono: full-mix downmix
            return
        if out_mono:
            col = int(out_lo)
            if 0 <= col < self._bus_channels:
                acc[start:end, col] += block.mean(axis=1)   # (seg,) broadcasts into the column
            return
        lo = int(out_lo)
        if 0 <= lo and lo + 2 <= self._bus_channels:
            acc[start:end, lo:lo + 2] += block
        # else: beyond bus width -> dropped (silent). No error.

    def _cleanup_finished(self) -> None:
        """Drop voices whose envelope has run out."""
        if self._normal is not None and self._normal.finished:
            self._normal = None
        for cue_id in [k for k, v in self._backgrounds.items() if v.finished]:
            del self._backgrounds[cue_id]
        if self._audition is not None and self._audition.finished:
            self._audition = None
        if self._dying:
            self._dying = [v for v in self._dying if not v.finished]

    # =====================================================================
    # Status
    # =====================================================================
    def get_status(self) -> dict:
        """Snapshot of the mixer state."""
        with self._lock:
            self._drain_locked()
            normal = None
            if self._normal is not None:
                v = self._normal
                normal = {
                    "cue_id": v.cue_id,
                    "frame": v.playhead,
                    "total_frames": v.total,
                    "finished": bool(v.finished),
                }
            backgrounds = [
                {
                    "cue_id": v.cue_id,
                    "frame": v.playhead,
                    "total_frames": v.total,
                    "loop": bool(v.loop),
                }
                for v in self._backgrounds.values()
            ]
            audition = None
            if self._audition is not None:
                v = self._audition
                audition = {
                    "cue_id": v.cue_id,
                    "frame": v.playhead,
                    "total_frames": v.total,
                    "finished": bool(v.finished),
                }
            return {
                "normal": normal,
                "backgrounds": backgrounds,
                "audition": audition,
                "audition_active": self._audition is not None,
                "device_ok": bool(self.device_ok),
                "output_channels": int(self._output_channels),
                "bus_channels": int(self._bus_channels),
                "paused": bool(self._paused),
                "scheduled": [
                    {
                        "cue_id": sf.cue_id,
                        "remaining_frames": int(max(0, sf.remaining)),
                        "kind": sf.kind,
                    }
                    for sf in self._scheduled
                ],
            }

    # =====================================================================
    # Output device control (real playback; not exercised by headless tests)
    # =====================================================================
    @staticmethod
    def list_output_devices() -> list[dict]:
        """Enumerate output-capable devices via sounddevice."""
        import sounddevice as sd

        try:
            default_out = sd.default.device[1]
        except Exception:
            default_out = None

        devices = []
        for index, info in enumerate(sd.query_devices()):
            if info.get("max_output_channels", 0) <= 0:
                continue
            devices.append(
                {
                    "index": index,
                    "name": info.get("name", f"device {index}"),
                    "max_output_channels": int(info.get("max_output_channels", 0)),
                    "default": index == default_out,
                }
            )
        return devices

    def start_output(self, device=None) -> None:
        """Open a sounddevice OutputStream driven by :meth:`render`."""
        self._stop_requested = False
        self._reconnect_stop.clear()
        self._device = device
        self._open_stream()

    def stop_output(self) -> None:
        """Stop and close the output stream and any reconnect attempts."""
        self._stop_requested = True
        self._reconnect_stop.set()
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        # Release the soxr resampler now that no callback can run: a live
        # CSoxr instance at interpreter exit makes nanobind print leak
        # warnings all over the console during a shutdown/self-update.
        self._resampler = None

    def _open_stream(self) -> None:
        import sounddevice as sd

        try:
            rate, out_ch = self._resolve_output_format(sd)
            rate = self._setup_resampler(rate, out_ch)
            stream = sd.OutputStream(
                samplerate=rate,
                channels=out_ch,
                dtype="float32",
                device=self._device,
                callback=self._audio_callback,
                finished_callback=self._on_stream_finished,
            )
            stream.start()
            self._stream = stream
            self.device_ok = True
        except Exception:
            # Device missing/busy: keep show state, never fall back to another
            # device, and keep trying to reopen the SAME device.
            self.device_ok = False
            self._stream = None
            self._start_reconnect()

    def _resolve_output_format(self, sd) -> tuple[int, int]:
        """Native (samplerate, channels) of the selected output device.

        Returns the device's real rate so the stream is opened to match its clock
        (see :meth:`_open_stream`), and the device's usable output width (1 for a
        mono device, else capped at 32); the mix bus is opened to match.
        """
        index = self._device
        if index is None:
            index = sd.default.device[1]
        info = sd.query_devices(index)
        rate = int(round(info.get("default_samplerate", SAMPLE_RATE))) or SAMPLE_RATE
        max_out = int(info.get("max_output_channels", CHANNELS))
        out_ch = 1 if max_out <= 1 else min(32, max_out)
        return rate, out_ch

    def _setup_resampler(self, rate: int, out_ch: int) -> int:
        """Prepare the realtime resampler for ``rate``; return the rate to open at.

        Derives the mix-bus width from ``out_ch`` (a mono device keeps a 2-wide
        bus and outputs 1 channel; otherwise the bus matches the device width).
        Channel mapping to the device happens AFTER resample in
        :meth:`_to_output_channels`, so the resampler runs at the bus width.

        If the device rate matches the engine rate no resampler is needed. If a
        resampler is unavailable (soxr missing), fall back to opening at the engine
        rate so the stream still works (best effort, may play at wrong speed).
        """
        mono = out_ch <= 1
        bus = 2 if mono else out_ch
        # Build the resampler (if needed) BEFORE taking the lock; only the width
        # field writes must be atomic vs. the audio callback (see AMENDMENT).
        resampler = None
        open_rate = SAMPLE_RATE
        if rate != SAMPLE_RATE:
            try:
                import soxr

                resampler = soxr.ResampleStream(
                    SAMPLE_RATE, rate, bus, dtype="float32", quality="HQ"
                )
                open_rate = rate
            except Exception:
                resampler = None
                open_rate = SAMPLE_RATE
        with self._lock:
            self._mono_out = mono
            self._bus_channels = bus
            self._output_channels = out_ch
            self._resampler = resampler
            self._output_rate = open_rate
            self._resample_buf = np.zeros((0, self._bus_channels), dtype=NP_DTYPE)
        return open_rate

    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        import sounddevice as sd

        try:
            if status:
                # Underflows/overflows are non-fatal; keep playing.
                pass
            outdata[:] = self._produce_output(frames)
        except Exception:
            outdata[:] = 0
            self.device_ok = False
            raise sd.CallbackAbort

    def _produce_output(self, frames: int) -> np.ndarray:
        """Render ``frames`` of audio in the device's format (rate + channels).

        With no resampler this is the engine mix directly. Otherwise the engine is
        rendered and pushed through the stateful resampler until ``frames`` of
        output-rate audio are buffered; the remainder carries to the next block so
        no samples are dropped and the filter state stays continuous (glitch-free).
        """
        if self._resampler is None:
            return self._to_output_channels(self.render(frames))

        buf = self._resample_buf
        while len(buf) < frames:
            need = frames - len(buf)
            in_frames = max(1, int(np.ceil(need * SAMPLE_RATE / self._output_rate)))
            chunk = self._resampler.resample_chunk(self.render(in_frames))
            if len(chunk):
                buf = chunk if len(buf) == 0 else np.concatenate((buf, chunk), axis=0)
        out = buf[:frames]
        self._resample_buf = buf[frames:]
        return self._to_output_channels(out)

    def _to_output_channels(self, block: np.ndarray) -> np.ndarray:
        """Map a bus-width mix block (n, _bus_channels) to the device width."""
        if self._mono_out:
            return block.mean(axis=1, keepdims=True).astype(NP_DTYPE)   # (n,1) full-bus mean
        if self._output_channels == self._bus_channels:
            return block
        if self._output_channels > self._bus_channels:                  # defensive zero-pad
            out = np.zeros((block.shape[0], self._output_channels), dtype=NP_DTYPE)
            out[:, :self._bus_channels] = block
            return out
        return block[:, :self._output_channels]                          # defensive narrow

    def _on_stream_finished(self) -> None:
        # Called when the stream stops. If we did not ask for it, the device was
        # lost -- flag it and start trying to reopen the same device.
        if self._stop_requested:
            return
        self.device_ok = False
        self._stream = None
        self._start_reconnect()

    def _start_reconnect(self) -> None:
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return
        self._reconnect_stop.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, name="cueforge-audio-reconnect", daemon=True
        )
        self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        while not self._reconnect_stop.is_set() and not self._stop_requested:
            time.sleep(1.0)
            if self._stop_requested or self._reconnect_stop.is_set():
                return
            self._open_stream()
            if self.device_ok:
                return
