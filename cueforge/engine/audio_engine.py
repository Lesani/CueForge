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
    seconds_to_frames,
)
from cueforge.engine.voice import Voice

# Ramp lengths.
DECLICK_SECONDS = 0.010   # ~10 ms hard-cut declick
PANIC_SECONDS = 0.150     # ~150 ms panic fade

# Simultaneous voice cap (~32 simultaneous voices).
MAX_BACKGROUNDS = 32


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

    def __init__(self) -> None:
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS

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
        self._output_channels = CHANNELS
        self._resampler = None
        self._resample_buf = np.zeros((0, CHANNELS), dtype=NP_DTYPE)

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
    # Public control API
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
        )

        def cmd() -> None:
            if self._normal is not None:
                self._normal.begin_kill(self._declick_frames)
                self._dying.append(self._normal)
            self._normal = voice

        self._enqueue(cmd)

    def play_background(
        self,
        cue_id: str,
        pcm: np.ndarray,
        *,
        gain_db: float = 0.0,
        fade_in: float = 0.0,
        loop: bool = False,
        fade_shape: str = "linear",
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
        )

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

        self._enqueue(cmd)

    def stop_background(
        self,
        cue_id: str,
        *,
        mode: str = "fade",
        fade_seconds: float = 2.0,
    ) -> None:
        """Stop one background: ``mode="hard"`` removes it immediately,
        ``mode="fade"`` ramps it to silence over ``fade_seconds``."""

        def cmd() -> None:
            voice = self._backgrounds.pop(cue_id, None)
            if voice is None:
                return
            if mode == "hard":
                return  # immediate: simply dropped, not rendered again
            voice.begin_kill(max(1, seconds_to_frames(fade_seconds)))
            self._dying.append(voice)

        self._enqueue(cmd)

    def stop_all_backgrounds(
        self,
        *,
        mode: str = "fade",
        fade_seconds: float = 2.0,
    ) -> None:
        """Stop every background (hard or fade)."""

        def cmd() -> None:
            voices = list(self._backgrounds.values())
            self._backgrounds.clear()
            if mode == "hard":
                return
            frames = max(1, seconds_to_frames(fade_seconds))
            for voice in voices:
                voice.begin_kill(frames)
                self._dying.append(voice)

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

        self._enqueue(cmd)

    # =====================================================================
    # Mixing -- single source of truth
    # =====================================================================
    def render(self, num_frames: int) -> np.ndarray:
        """Advance the mix by ``num_frames`` and return float32 (num_frames, 2)."""
        num_frames = int(num_frames)
        if num_frames <= 0:
            return np.zeros((0, CHANNELS), dtype=NP_DTYPE)

        with self._lock:
            self._drain_locked()

            acc = np.zeros((num_frames, CHANNELS), dtype=np.float64)

            if self._normal is not None:
                acc += self._normal.render(num_frames)
            for voice in self._backgrounds.values():
                acc += voice.render(num_frames)
            if self._audition is not None:
                acc += self._audition.render(num_frames)
            for voice in self._dying:
                acc += voice.render(num_frames)

            # Drop finished voices.
            if self._normal is not None and self._normal.finished:
                self._normal = None
            for cue_id in [k for k, v in self._backgrounds.items() if v.finished]:
                del self._backgrounds[cue_id]
            if self._audition is not None and self._audition.finished:
                self._audition = None
            if self._dying:
                self._dying = [v for v in self._dying if not v.finished]

        return _soft_limit(acc).astype(NP_DTYPE)

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
        (see :meth:`_open_stream`). Channels are clamped to the engine's stereo
        mix: stereo when the device supports it, otherwise mono.
        """
        index = self._device
        if index is None:
            index = sd.default.device[1]
        info = sd.query_devices(index)
        rate = int(round(info.get("default_samplerate", SAMPLE_RATE))) or SAMPLE_RATE
        max_out = int(info.get("max_output_channels", CHANNELS))
        out_ch = CHANNELS if max_out >= CHANNELS else max(1, max_out)
        return rate, out_ch

    def _setup_resampler(self, rate: int, out_ch: int) -> int:
        """Prepare the realtime resampler for ``rate``; return the rate to open at.

        If the device rate matches the engine rate no resampler is needed. If a
        resampler is unavailable (soxr missing), fall back to opening at the engine
        rate so the stream still works (best effort, may play at wrong speed).
        """
        self._output_channels = out_ch
        self._resample_buf = np.zeros((0, CHANNELS), dtype=NP_DTYPE)
        if rate == SAMPLE_RATE:
            self._resampler = None
            self._output_rate = SAMPLE_RATE
            return SAMPLE_RATE
        try:
            import soxr

            self._resampler = soxr.ResampleStream(
                SAMPLE_RATE, rate, CHANNELS, dtype="float32", quality="HQ"
            )
            self._output_rate = rate
            return rate
        except Exception:
            self._resampler = None
            self._output_rate = SAMPLE_RATE
            return SAMPLE_RATE

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
        """Map a stereo mix block to the device's channel count."""
        if self._output_channels == CHANNELS:
            return block
        if self._output_channels == 1:
            return block.mean(axis=1, keepdims=True).astype(NP_DTYPE)
        return block

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
