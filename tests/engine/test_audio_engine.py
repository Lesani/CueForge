# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Deep DSP/mix behavior tests for the AudioEngine.

All tests run fully offline via ``render()`` on numpy-synthesized signals; no
audio device and no files are involved.
"""

from __future__ import annotations

import numpy as np
import pytest

from cueforge.audio_format import CHANNELS, SAMPLE_RATE, db_to_gain
from cueforge.engine.audio_engine import PANIC_SECONDS, AudioEngine


# ---------------------------------------------------------------------------
# signal helpers
# ---------------------------------------------------------------------------
def tone(frames: int, freq: float, amp: float = 0.3, phase: float = 0.0) -> np.ndarray:
    """Stereo sine of ``frames`` samples starting at ``phase`` radians."""
    n = np.arange(frames)
    sig = (amp * np.sin(2.0 * np.pi * freq * n / SAMPLE_RATE + phase)).astype(np.float32)
    return np.column_stack([sig, sig])


def dc(frames: int, level: float = 1.0) -> np.ndarray:
    """Stereo constant-level buffer -- lets us read the envelope directly."""
    return np.full((frames, CHANNELS), level, dtype=np.float32)


def mono(block: np.ndarray) -> np.ndarray:
    return block[:, 0]


# ---------------------------------------------------------------------------
# normal channel: hard cut with declick
# ---------------------------------------------------------------------------
def test_normal_cut_replaces_content_with_declick():
    eng = AudioEngine()
    a = tone(SAMPLE_RATE, freq=200.0, amp=0.3)
    b = tone(SAMPLE_RATE, freq=400.0, amp=0.3)

    eng.play_normal("A", a)
    out1 = eng.render(4800)          # A only
    eng.play_normal("B", b)
    out2 = eng.render(4800)          # B, with A declicking out over ~10 ms

    # After the ~10 ms declick, only B remains: compare to a freshly generated
    # B slice (B started at the top of out2).
    expected_b = tone(4800, freq=400.0, amp=0.3)
    tail = slice(600, 4800)          # safely past the 480-frame declick
    assert np.allclose(out2[tail], expected_b[tail], atol=2e-3)

    # A's original content (200 Hz) is gone from the tail.
    expected_a_tail = tone(9600, freq=200.0, amp=0.3)[4800:]
    assert not np.allclose(out2[tail], expected_a_tail[tail], atol=1e-2)

    # No full-scale discontinuity across the cut boundary.
    joined = mono(np.vstack([out1, out2]))
    assert np.max(np.abs(np.diff(joined))) < 0.05


# ---------------------------------------------------------------------------
# background layering
# ---------------------------------------------------------------------------
def test_backgrounds_layer_and_survive_normal_cue():
    eng = AudioEngine()
    bg1 = tone(SAMPLE_RATE, freq=200.0, amp=0.25)
    bg2 = tone(SAMPLE_RATE, freq=330.0, amp=0.25)

    eng.play_background("bg1", bg1)
    eng.play_background("bg2", bg2)
    out = eng.render(4800)

    expected = tone(4800, 200.0, 0.25) + tone(4800, 330.0, 0.25)
    assert np.allclose(out, expected, atol=2e-3)          # both audible, summed

    status = eng.get_status()
    assert {b["cue_id"] for b in status["backgrounds"]} == {"bg1", "bg2"}

    # Firing a normal cue leaves both backgrounds running.
    eng.play_normal("N", tone(SAMPLE_RATE, 500.0, 0.2))
    eng.render(4800)
    status2 = eng.get_status()
    assert {b["cue_id"] for b in status2["backgrounds"]} == {"bg1", "bg2"}
    assert status2["normal"]["cue_id"] == "N"


def test_refiring_same_background_restarts_single_voice():
    eng = AudioEngine()
    buf = tone(SAMPLE_RATE, 200.0, 0.3)

    eng.play_background("x", buf)
    eng.render(24000)                # advance ~0.5 s
    assert eng.get_status()["backgrounds"][0]["frame"] >= 24000

    eng.play_background("x", buf)    # re-fire -> restart
    eng.render(480)
    status = eng.get_status()
    assert len(status["backgrounds"]) == 1          # still exactly one voice
    assert status["backgrounds"][0]["frame"] < 1000  # playhead reset to top


# ---------------------------------------------------------------------------
# seamless looping
# ---------------------------------------------------------------------------
def test_background_loops_seamlessly():
    length = 4800
    # 480 Hz over 4800 frames = exactly 48 whole cycles -> content wraps cleanly.
    buf = tone(length, freq=480.0, amp=0.3)

    eng = AudioEngine()
    eng.play_background("loop", buf, loop=True)
    out = eng.render(length * 3)

    # Never silent, and each loop repeats the previous one.
    assert np.max(np.abs(out)) > 0.1
    assert np.allclose(out[0:length], out[length:2 * length], atol=1e-4)
    assert np.allclose(out[length:2 * length], out[2 * length:3 * length], atol=1e-4)

    # No click at either loop boundary.
    assert np.max(np.abs(np.diff(mono(out)))) < 0.05


# ---------------------------------------------------------------------------
# fade-in ramp + fade shapes
# ---------------------------------------------------------------------------
def test_fade_in_ramp_starts_near_zero_and_rises():
    eng = AudioEngine()
    eng.play_normal("f", dc(4800), fade_in=0.1)   # 4800-frame fade over DC
    out = mono(eng.render(4800))

    assert out[0] < 0.01              # starts near silence
    assert out[-1] > 0.98            # reaches unity at the end
    # Monotonic rise (sample every 240 frames).
    sampled = out[::240]
    assert np.all(np.diff(sampled) >= -1e-6)


def test_equal_power_vs_linear_differ_at_midpoint():
    lin = AudioEngine()
    lin.play_normal("l", dc(4800), fade_in=0.1, fade_shape="linear")
    out_lin = mono(lin.render(4800))

    eqp = AudioEngine()
    eqp.play_normal("e", dc(4800), fade_in=0.1, fade_shape="equalPower")
    out_eqp = mono(eqp.render(4800))

    mid = 2400                        # halfway through the 4800-frame fade
    assert out_lin[mid] == pytest.approx(0.5, abs=1e-2)
    assert out_eqp[mid] == pytest.approx(np.sin(np.pi / 4), abs=1e-2)  # ~0.707
    assert out_eqp[mid] > out_lin[mid]


# ---------------------------------------------------------------------------
# stopping backgrounds
# ---------------------------------------------------------------------------
def test_stop_background_fade_ramps_to_silence():
    eng = AudioEngine()
    eng.play_background("b", dc(4800), loop=True)
    eng.render(4800)                                  # steady at unity

    eng.stop_background("b", mode="fade", fade_seconds=0.2)  # 9600-frame fade
    out = mono(eng.render(9600))

    assert out[0] == pytest.approx(1.0, abs=1e-2)     # starts at full
    assert out[4800] == pytest.approx(0.5, abs=2e-2)  # ~half-way down (linear)
    assert abs(out[-1]) < 1e-2                         # ends silent
    assert eng.get_status()["backgrounds"] == []


def test_stop_background_hard_is_immediate():
    eng = AudioEngine()
    eng.play_background("b", dc(4800), loop=True)
    eng.render(4800)

    eng.stop_background("b", mode="hard")
    out = eng.render(480)
    assert np.max(np.abs(out)) < 1e-6                 # instantly silent
    assert eng.get_status()["backgrounds"] == []


# ---------------------------------------------------------------------------
# panic
# ---------------------------------------------------------------------------
def test_panic_fades_everything_to_silence():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.play_background("b", dc(SAMPLE_RATE), loop=True)
    eng.render(4800)

    eng.panic()
    panic_frames = int(round(PANIC_SECONDS * SAMPLE_RATE))
    eng.render(panic_frames)          # consume the ~150 ms panic fade
    after = eng.render(480)           # everything should now be gone

    assert np.max(np.abs(after)) < 1e-6
    status = eng.get_status()
    assert status["normal"] is None
    assert status["backgrounds"] == []


# ---------------------------------------------------------------------------
# gain
# ---------------------------------------------------------------------------
def test_gain_minus_6db_halves_amplitude():
    eng = AudioEngine()
    eng.play_normal("g", dc(4800), gain_db=-6.0206)   # -6.02 dB -> x0.5
    out = mono(eng.render(4800))
    assert db_to_gain(-6.0206) == pytest.approx(0.5, abs=1e-4)
    assert np.allclose(out, 0.5, atol=1e-3)


# ---------------------------------------------------------------------------
# output-format adaptation (device rate/channels) -- regression for garbled,
# double-speed playback on devices whose native rate is not the engine's 48 kHz
# ---------------------------------------------------------------------------
class _FakeSD:
    """Minimal stand-in for the sounddevice module used by _resolve_output_format."""

    class default:
        device = (None, 3)   # (input, output) default indices

    def __init__(self, samplerate=44100.0, max_out=2):
        self._sr = samplerate
        self._max_out = max_out

    def query_devices(self, index):
        return {"default_samplerate": self._sr, "max_output_channels": self._max_out}


def test_resolve_output_format_uses_device_native_rate_and_clamps_channels():
    eng = AudioEngine()
    eng._device = 7
    rate, ch = eng._resolve_output_format(_FakeSD(samplerate=44100.0, max_out=8))
    assert rate == 44100      # device rate, NOT the hardcoded 48 kHz
    assert ch == 2            # stereo mix clamped down from an 8-channel device

    rate, ch = eng._resolve_output_format(_FakeSD(samplerate=96000.0, max_out=1))
    assert rate == 96000
    assert ch == 1            # mono device -> mono output


def test_setup_resampler_only_engages_on_rate_mismatch():
    eng = AudioEngine()
    assert eng._setup_resampler(SAMPLE_RATE, 2) == SAMPLE_RATE
    assert eng._resampler is None                     # no conversion needed

    open_rate = eng._setup_resampler(44100, 2)
    assert open_rate == 44100                          # open at the device rate
    assert eng._resampler is not None                  # resampler engaged


def test_produce_output_preserves_pitch_at_device_rate():
    """A 1 kHz tone must still read as ~1 kHz after resampling to 44.1 kHz.

    The original bug fed 48 kHz samples to a device clocked at another rate, so
    the tone came out shifted (roughly double speed) and garbled. Here the mix is
    resampled to the device rate, so the pitch is preserved.
    """
    out_rate = 44100
    eng = AudioEngine()
    eng._setup_resampler(out_rate, 2)
    eng.play_normal("t", tone(2 * SAMPLE_RATE, freq=1000.0, amp=0.5))

    pulled = []
    while sum(len(b) for b in pulled) < out_rate:      # ~1 s at the device rate
        block = eng._produce_output(1024)
        assert block.shape[1] == 2
        pulled.append(block)

    sig = np.concatenate(pulled)[:out_rate, 0]
    spectrum = np.abs(np.fft.rfft(sig * np.hanning(len(sig))))
    freqs = np.fft.rfftfreq(len(sig), 1.0 / out_rate)
    peak = freqs[1 + int(np.argmax(spectrum[1:]))]
    assert peak == pytest.approx(1000.0, abs=15.0)     # not ~2000 Hz (double speed)


def test_produce_output_downmixes_to_mono_device():
    eng = AudioEngine()
    eng._setup_resampler(SAMPLE_RATE, 1)               # mono device, no resample
    eng.play_normal("m", dc(4800, level=0.4))
    block = eng._produce_output(1024)
    assert block.shape == (1024, 1)
    assert np.allclose(block, 0.4, atol=1e-3)


# ---------------------------------------------------------------------------
# audition playhead reporting (drives the Library waveform marker)
# ---------------------------------------------------------------------------
def test_audition_status_reports_advancing_playhead():
    eng = AudioEngine()
    eng.audition(tone(SAMPLE_RATE, freq=300.0))        # 1 s, non-loop

    st = eng.get_status()                              # drains the queued command
    assert st["audition"] is not None
    assert st["audition"]["frame"] == 0
    assert st["audition"]["total_frames"] == SAMPLE_RATE
    assert st["audition_active"] is True

    eng.render(4800)
    assert eng.get_status()["audition"]["frame"] >= 4799   # playhead advanced


def test_stop_audition_clears_status():
    eng = AudioEngine()
    eng.audition(tone(SAMPLE_RATE, freq=300.0))
    assert eng.get_status()["audition"] is not None

    eng.stop_audition()
    st = eng.get_status()                              # drains the stop command
    assert st["audition"] is None
    assert st["audition_active"] is False
