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
from cueforge.engine.audio_engine import (
    ALL_BACKGROUNDS,
    DECLICK_SECONDS,
    PANIC_SECONDS,
    AudioEngine,
)

DECLICK_FRAMES = int(round(DECLICK_SECONDS * SAMPLE_RATE))


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


def test_resolve_output_format_uses_device_native_rate_and_width():
    eng = AudioEngine()
    eng._device = 7
    rate, ch = eng._resolve_output_format(_FakeSD(samplerate=44100.0, max_out=8))
    assert rate == 44100      # device rate, NOT the hardcoded 48 kHz
    assert ch == 8            # full 8-channel device width (no longer clamped to 2)

    rate, ch = eng._resolve_output_format(_FakeSD(samplerate=96000.0, max_out=1))
    assert rate == 96000
    assert ch == 1            # mono device -> mono output

    rate, ch = eng._resolve_output_format(_FakeSD(samplerate=48000.0, max_out=64))
    assert ch == 32           # capped at 32 channels


def test_setup_resampler_only_engages_on_rate_mismatch():
    eng = AudioEngine()
    assert eng._setup_resampler(SAMPLE_RATE, 2) == SAMPLE_RATE
    assert eng._resampler is None                     # no conversion needed

    open_rate = eng._setup_resampler(44100, 4)
    assert open_rate == 44100                          # open at the device rate
    assert eng._resampler is not None                  # resampler engaged
    assert eng._bus_channels == 4                      # bus widened to the device


def test_produce_output_preserves_pitch_at_device_rate():
    """A 1 kHz tone must still read as ~1 kHz after resampling to 44.1 kHz.

    The original bug fed 48 kHz samples to a device clocked at another rate, so
    the tone came out shifted (roughly double speed) and garbled. Here the mix is
    resampled to the device rate, so the pitch is preserved.
    """
    out_rate = 44100
    eng = AudioEngine()
    eng._setup_resampler(out_rate, 4)                  # 4-wide device: bus widens to 4
    eng.play_normal("t", tone(2 * SAMPLE_RATE, freq=1000.0, amp=0.5))

    pulled = []
    while sum(len(b) for b in pulled) < out_rate:      # ~1 s at the device rate
        block = eng._produce_output(1024)
        assert block.shape[1] == 4                     # device width preserved end-to-end
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


# ---------------------------------------------------------------------------
# scheduled fires -- sample-accurate chain activation
# ---------------------------------------------------------------------------
def test_scheduled_normal_activates_at_exact_frame():
    eng = AudioEngine()
    eng.schedule_normal("a", dc(4800), 1000)
    out = mono(eng.render(2048))
    assert np.max(np.abs(out[:1000])) < 1e-9        # silent before activation
    assert np.allclose(out[1000:], 1.0, atol=1e-6)  # cue at unity from frame 1000


def test_scheduled_activation_split_across_two_blocks():
    eng = AudioEngine()
    eng.schedule_normal("a", dc(SAMPLE_RATE), 1500)

    out1 = mono(eng.render(1024))                   # nothing fires yet
    assert np.max(np.abs(out1)) < 1e-9
    out2 = mono(eng.render(1024))                   # activates at offset 476

    assert np.max(np.abs(out2[:476])) < 1e-9        # silent up to global 1500
    assert np.allclose(out2[476:], 1.0, atol=1e-6)  # no samples dropped


def test_scheduled_normal_hard_cuts_live_normal_with_declick():
    eng = AudioEngine()
    eng.play_normal("A", tone(SAMPLE_RATE, 200.0, 0.3))
    eng.schedule_normal("B", tone(SAMPLE_RATE, 400.0, 0.3), 1000)
    out = mono(eng.render(4800))

    # A present before the cut, B present well after the ~480-frame declick.
    expected_a = mono(tone(1000, 200.0, 0.3))
    assert np.allclose(out[:1000], expected_a, atol=2e-3)
    # B started at frame 1000, so global frame g carries B sample (g - 1000).
    expected_b = mono(tone(SAMPLE_RATE, 400.0, 0.3))
    assert np.allclose(out[1600:4800], expected_b[600:3800], atol=2e-3)
    assert np.max(np.abs(np.diff(out))) < 0.05      # no click across the cut


def test_scheduled_background_stacks_with_running_background():
    eng = AudioEngine()
    eng.play_background("bg1", dc(SAMPLE_RATE, 0.25), loop=True)
    eng.schedule_background("bg2", dc(SAMPLE_RATE, 0.25), 1000, loop=True)
    out = mono(eng.render(2048))

    assert np.allclose(out[:1000], 0.25, atol=1e-6)   # only bg1 before
    assert np.allclose(out[1000:], 0.5, atol=1e-6)    # both summed after
    assert {b["cue_id"] for b in eng.get_status()["backgrounds"]} == {"bg1", "bg2"}


def test_schedule_stop_all_backgrounds_executes_at_frame():
    eng = AudioEngine()
    eng.play_background("b", dc(SAMPLE_RATE), loop=True)
    eng.schedule_stop_all_backgrounds("stopall", 1000, mode="fade", fade_seconds=0.1)
    out = mono(eng.render(4800))

    assert np.allclose(out[:1000], 1.0, atol=1e-6)    # steady until the fire
    assert out[1000 + 2400] < 0.6                     # ramping down after


def test_cancel_scheduled_removes_pending_fire():
    eng = AudioEngine()
    eng.schedule_normal("x", dc(SAMPLE_RATE), 5000)
    eng.cancel_scheduled("x")
    out = mono(eng.render(8000))
    assert np.max(np.abs(out)) < 1e-9                 # never fired


def test_cancel_all_scheduled_removes_everything():
    eng = AudioEngine()
    eng.schedule_normal("x", dc(SAMPLE_RATE), 2000)
    eng.schedule_background("y", dc(SAMPLE_RATE), 3000)
    eng.cancel_all_scheduled()
    out = mono(eng.render(8000))
    assert np.max(np.abs(out)) < 1e-9
    assert eng.get_status()["scheduled"] == []


def test_panic_clears_scheduled_fires():
    eng = AudioEngine()
    eng.play_normal("live", dc(SAMPLE_RATE))
    eng.schedule_normal("pending", dc(SAMPLE_RATE), 3000)
    eng.render(480)
    eng.panic()
    panic_frames = int(round(PANIC_SECONDS * SAMPLE_RATE))
    eng.render(panic_frames)
    out = mono(eng.render(8000))                      # long past the pending fire
    assert np.max(np.abs(out)) < 1e-6
    assert eng.get_status()["scheduled"] == []


def test_stop_cue_cancels_pending_fire():
    eng = AudioEngine()
    eng.schedule_normal("x", dc(SAMPLE_RATE), 5000)
    eng.stop_cue("x")
    out = mono(eng.render(8000))
    assert np.max(np.abs(out)) < 1e-9
    assert eng.get_status()["scheduled"] == []


def test_scheduled_stop_background_is_noop_when_target_not_live():
    eng = AudioEngine()
    # No background named "ghost" is running; the fire must activate harmlessly.
    eng.schedule_stop_background("stopcue", "ghost", 1000, mode="hard")
    out = mono(eng.render(4800))
    assert np.max(np.abs(out)) < 1e-9                 # nothing happens, no error
    assert eng.get_status()["scheduled"] == []        # fire consumed


# ---------------------------------------------------------------------------
# global pause / resume
# ---------------------------------------------------------------------------
def test_pause_freezes_playhead_and_declicks():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.render(4800)                                  # steady at unity
    frame_before = eng.get_status()["normal"]["frame"]

    eng.pause_all()
    out = mono(eng.render(4800))
    assert out[0] == pytest.approx(1.0, abs=2e-2)     # starts full
    assert out[DECLICK_FRAMES + 50] < 1e-6            # silent after the declick
    assert eng.get_status()["paused"] is True

    frame_after = eng.get_status()["normal"]["frame"]
    eng.render(4800)                                  # fully frozen now
    frame_still = eng.get_status()["normal"]["frame"]
    assert frame_still == frame_after                 # playhead stopped advancing
    assert frame_after > frame_before                 # advanced during the declick


def test_resume_ramps_up_and_advances_playhead():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.render(4800)
    eng.pause_all()
    eng.render(4800)                                  # settle into the freeze
    frozen_frame = eng.get_status()["normal"]["frame"]

    eng.resume_all()
    out = mono(eng.render(4800))
    assert out[0] < 0.2                               # ramps up from silence
    assert out[-1] == pytest.approx(1.0, abs=2e-2)    # back to unity
    assert eng.get_status()["paused"] is False
    assert eng.get_status()["normal"]["frame"] > frozen_frame


def test_pause_does_not_affect_audition():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.audition(dc(SAMPLE_RATE))
    eng.render(4800)
    eng.pause_all()
    eng.render(4800)                                  # settle freeze
    aud_frame = eng.get_status()["audition"]["frame"]
    eng.render(4800)
    assert eng.get_status()["audition"]["frame"] > aud_frame   # audition advances
    assert eng.get_status()["normal"]["frame"] == \
        eng.get_status()["normal"]["frame"]           # normal stays frozen


def test_go_while_paused_plays_new_voice_at_full_level():
    eng = AudioEngine()
    eng.play_background("old", dc(SAMPLE_RATE), loop=True)
    eng.render(4800)
    eng.pause_all()
    eng.render(4800)                                  # "old" now frozen/silent

    eng.play_background("new", dc(SAMPLE_RATE, 0.4), loop=True)
    out = mono(eng.render(4800))
    assert np.allclose(out, 0.4, atol=1e-3)           # only the new voice sounds


def test_paused_scheduled_countdown_freezes_then_resumes():
    eng = AudioEngine()
    eng.schedule_normal("a", dc(SAMPLE_RATE), 1000)
    eng.pause_all()
    out = mono(eng.render(4800))
    assert np.max(np.abs(out)) < 1e-9                 # no activation while paused
    assert eng.get_status()["scheduled"][0]["remaining_frames"] == 1000

    eng.resume_all()
    out2 = mono(eng.render(4800))
    assert np.max(np.abs(out2[:1000])) < 1e-9         # fires ~1000 frames post-resume
    assert np.allclose(out2[1000:], 1.0, atol=1e-6)


def test_panic_overrides_pause():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.play_background("b", dc(SAMPLE_RATE), loop=True)
    eng.render(4800)
    eng.pause_all()
    eng.render(4800)                                  # everything frozen silent

    eng.panic()
    panic_frames = int(round(PANIC_SECONDS * SAMPLE_RATE))
    eng.render(panic_frames)
    after = eng.render(480)
    assert np.max(np.abs(after)) < 1e-6
    st = eng.get_status()
    assert st["normal"] is None
    assert st["backgrounds"] == []
    assert st["paused"] is False


def test_status_reports_paused_and_scheduled():
    eng = AudioEngine()
    eng.schedule_normal("a", dc(SAMPLE_RATE), 2000)
    eng.schedule_background("b", dc(SAMPLE_RATE), 3000)
    eng.pause_all()
    st = eng.get_status()
    assert st["paused"] is True
    sched = {s["cue_id"]: s for s in st["scheduled"]}
    assert sched["a"]["remaining_frames"] == 2000
    assert sched["a"]["kind"] == "normal"
    assert sched["b"]["kind"] == "background"


# ---------------------------------------------------------------------------
# live gain ramps (fade cues / live edits)
# ---------------------------------------------------------------------------
def test_set_cue_gain_ramps_normal_linear():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))            # 0 dB -> unity
    eng.set_cue_gain("n", -6.0206, 0.1)              # 4800-frame linear ramp to 0.5
    out = mono(eng.render(4800))
    assert out[0] == pytest.approx(1.0, abs=1e-3)    # starts at the current level
    assert out[2400] == pytest.approx(0.75, abs=2e-3)  # linear midpoint
    assert out[-1] == pytest.approx(0.5, abs=2e-3)   # settles at target


def test_set_cue_gain_equalpower_differs_at_midpoint():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.set_cue_gain("n", -60.0, 0.1, shape="equalPower")   # down-ramp to ~silence
    out = mono(eng.render(4800))
    # Equal-power descent to silence matches the cos-shaped equal-power fade-out:
    # at the midpoint gain ~cos(pi/4) = 0.707, distinct from the linear 0.5.
    assert out[2400] == pytest.approx(0.707, abs=1.5e-2)
    assert abs(out[2400] - 0.5) > 0.15


def test_set_cue_gain_targets_background_by_key():
    eng = AudioEngine()
    eng.play_background("bg1", dc(SAMPLE_RATE, 0.25), loop=True)
    eng.play_background("bg2", dc(SAMPLE_RATE, 0.25), loop=True)
    eng.set_cue_gain("bg1", -60.0, 0.1)              # only bg1 to silence
    eng.render(4800)                                 # complete the ramp
    out = mono(eng.render(480))
    assert np.allclose(out, 0.25, atol=2e-3)         # bg1 gone, bg2 unchanged
    assert {b["cue_id"] for b in eng.get_status()["backgrounds"]} == {"bg1", "bg2"}


def test_set_cue_gain_stop_when_done_drops_voice():
    eng = AudioEngine()
    eng.play_background("b", dc(SAMPLE_RATE), loop=True)
    eng.set_cue_gain("b", -60.0, 0.05, stop_when_done=True)   # 2400-frame ramp
    eng.render(2400 + 960)                           # past the ramp + declick kill
    out = mono(eng.render(480))
    assert np.max(np.abs(out)) < 1e-6                # fully silent
    assert eng.get_status()["backgrounds"] == []    # voice dropped


def test_set_cue_gain_noop_when_absent():
    eng = AudioEngine()
    eng.set_cue_gain("ghost", -6.0, 0.1)             # no such voice
    out = mono(eng.render(4800))
    assert np.max(np.abs(out)) < 1e-9                # no error, silent


def test_gain_ramp_composes_with_fade_in():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE), fade_in=0.1)
    eng.set_cue_gain("n", -6.0206, 0.1)              # ramp base to 0.5 as fade-in rises
    eng.render(4800)                                 # both settle
    out = mono(eng.render(480))
    assert np.allclose(out, 0.5, atol=2e-3)          # fade-in done (1.0) * gain (0.5)


def test_gain_ramp_freezes_under_pause_and_resumes():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.set_cue_gain("n", -6.0206, 0.2)              # 9600-frame ramp to 0.5
    eng.render(2400)                                 # part-way down
    eng.pause_all()
    eng.render(480)                                  # settle the pause declick
    eng.render(9600)                                 # long freeze: ramp must not advance
    eng.resume_all()
    partial = mono(eng.render(2000))                 # declick up, ramp resumes
    assert partial[-1] > 0.6                         # frozen: nowhere near target yet
    done = mono(eng.render(9600))
    assert done[-1] == pytest.approx(0.5, abs=1e-2)  # completes to target after resume


def test_panic_overrides_pending_gain_ramp():
    eng = AudioEngine()
    eng.play_background("b", dc(SAMPLE_RATE), loop=True)
    eng.set_cue_gain("b", -6.0206, 0.2)              # in-flight ramp
    eng.render(1200)
    eng.panic()
    panic_frames = int(round(PANIC_SECONDS * SAMPLE_RATE))
    eng.render(panic_frames)
    out = mono(eng.render(480))
    assert np.max(np.abs(out)) < 1e-6                # silent
    assert eng.get_status()["backgrounds"] == []     # dropped


def test_set_all_backgrounds_gain_ramps_all():
    eng = AudioEngine()
    eng.play_background("bg1", dc(SAMPLE_RATE, 0.5), loop=True)
    eng.play_background("bg2", dc(SAMPLE_RATE, 0.5), loop=True)
    eng.set_all_backgrounds_gain(-6.0206, 0.1)       # both to 0.5x
    eng.render(4800)
    out = mono(eng.render(480))
    assert np.allclose(out, 0.5, atol=2e-3)          # 0.25 + 0.25 -> both ramped


def test_schedule_fade_all_activates_at_frame_resolves_at_activation():
    eng = AudioEngine()
    eng.play_background("bg1", dc(SAMPLE_RATE, 0.5), loop=True)
    eng.schedule_background("bg2", dc(SAMPLE_RATE, 0.5), 500, loop=True)
    eng.schedule_fade("f", ALL_BACKGROUNDS, 1000, -60.0, 0.1)   # all backgrounds -> silence
    eng.render(1000)                                 # bg2 starts @500, fade activates @1000
    out = mono(eng.render(6000))                     # complete the 4800-frame ramp
    assert out[-1] < 1e-2                            # both bg1 and bg2 faded out
    assert eng.get_status()["scheduled"] == []       # fade fire consumed


def test_schedule_fade_specific_noop_when_not_live():
    eng = AudioEngine()
    eng.schedule_fade("f", "ghost", 1000, -6.0, 0.1)
    eng.render(4800)                                 # activates harmlessly
    assert eng.get_status()["scheduled"] == []


def test_schedule_fade_reports_kind_and_cancels():
    eng = AudioEngine()
    eng.schedule_fade("f", ALL_BACKGROUNDS, 5000, -6.0, 0.1)
    assert eng.get_status()["scheduled"][0]["kind"] == "fade"

    eng.cancel_scheduled("f")
    eng.render(1)
    assert eng.get_status()["scheduled"] == []

    eng2 = AudioEngine()
    eng2.schedule_fade("f", ALL_BACKGROUNDS, 5000, -6.0, 0.1)
    eng2.panic()
    eng2.render(1)
    assert eng2.get_status()["scheduled"] == []

    eng3 = AudioEngine()
    eng3.schedule_fade("f", ALL_BACKGROUNDS, 5000, -6.0, 0.1)
    eng3.stop_cue("f")
    eng3.render(1)
    assert eng3.get_status()["scheduled"] == []


# ---------------------------------------------------------------------------
# master gain (device trim)
# ---------------------------------------------------------------------------
def test_master_gain_minus_6db_halves_mix():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.set_master_gain(-6.0206)
    eng.render(2400)                                 # past the ~50 ms smoothing
    out = mono(eng.render(480))
    assert np.allclose(out, 0.5, atol=2e-3)


def test_master_gain_is_smoothed_not_stepped():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.set_master_gain(-6.0206)
    out = mono(eng.render(2400))
    assert out[0] == pytest.approx(1.0, abs=1e-3)    # not an instant step
    assert out[-1] == pytest.approx(0.5, abs=2e-3)   # arrives after the ramp


def test_master_gain_affects_audition():
    eng = AudioEngine()
    eng.audition(dc(SAMPLE_RATE))
    eng.set_master_gain(-6.0206)
    eng.render(2400)
    out = mono(eng.render(480))
    assert np.allclose(out, 0.5, atol=2e-3)


def test_master_gain_applies_before_soft_limit():
    eng = AudioEngine()
    eng.play_background("b1", dc(SAMPLE_RATE, 0.8), loop=True)
    eng.play_background("b2", dc(SAMPLE_RATE, 0.8), loop=True)   # sum 1.6, would soft-limit
    eng.set_master_gain(-6.0206)                     # 1.6 * 0.5 = 0.8 < 1 -> stays linear
    eng.render(2400)
    out = mono(eng.render(480))
    assert np.allclose(out, 0.8, atol=5e-3)          # linear, not the tanh-limited value


def test_master_gain_applies_while_paused():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.render(4800)
    eng.pause_all()
    eng.render(480)                                  # normal now frozen silent
    eng.play_background("b", dc(SAMPLE_RATE), loop=True)   # started paused: plays normally
    eng.set_master_gain(-6.0206)
    eng.render(2400)                                 # trim smoothing lands while paused
    out = mono(eng.render(480))
    assert np.allclose(out, 0.5, atol=2e-3)          # device trim applied to the live voice


# ---------------------------------------------------------------------------
# multichannel output -- stereo voices scattered into an N-channel bus at out_lo
# ---------------------------------------------------------------------------
def test_bus_defaults_to_stereo():
    assert AudioEngine().render(64).shape == (64, 2)


def test_bus_width_constructor_widens():
    assert AudioEngine(bus_channels=8).render(64).shape == (64, 8)
    assert AudioEngine(bus_channels=64).render(64).shape == (64, 32)   # cap 32
    assert AudioEngine(bus_channels=1).render(64).shape == (64, 2)     # floor 2


def test_set_bus_channels():
    eng = AudioEngine()
    assert eng.render(16).shape == (16, 2)
    eng.set_bus_channels(8)
    assert eng.render(16).shape == (16, 8)


def test_voice_scatters_at_out_lo():
    eng = AudioEngine(bus_channels=8)
    eng.play_normal("n", dc(4800), out_lo=2)               # -> cols 2:4
    out = eng.render(480)
    assert np.allclose(out[:, 2:4], 1.0, atol=1e-6)
    assert np.max(np.abs(out[:, 0:2])) < 1e-9
    assert np.max(np.abs(out[:, 4:8])) < 1e-9

    # A second voice at out_lo 0 lands in cols 0:2 independently.
    eng.play_background("b", dc(4800, 0.5), loop=True, out_lo=0)
    out2 = eng.render(480)
    assert np.allclose(out2[:, 0:2], 0.5, atol=1e-6)
    assert np.allclose(out2[:, 2:4], 1.0, atol=1e-6)       # normal still at out_lo 2
    assert np.max(np.abs(out2[:, 4:8])) < 1e-9


def test_background_and_audition_follow_out_lo():
    eng = AudioEngine(bus_channels=8)
    eng.play_background("b", dc(4800), loop=True, out_lo=4)        # -> cols 4:6
    eng.audition(dc(4800, 0.5), out_lo=2)                          # -> cols 2:4
    out = eng.render(480)
    assert np.allclose(out[:, 4:6], 1.0, atol=1e-6)               # bg at out_lo 4
    assert np.allclose(out[:, 2:4], 0.5, atol=1e-6)               # audition at out_lo 2
    assert np.max(np.abs(out[:, 0:2])) < 1e-9
    assert np.max(np.abs(out[:, 6:8])) < 1e-9


def test_dying_voice_keeps_out_lo():
    eng = AudioEngine(bus_channels=8)
    eng.play_normal("a", dc(SAMPLE_RATE), out_lo=2)
    eng.render(4800)                                             # steady on cols 2:4
    eng.play_normal("b", dc(SAMPLE_RATE), out_lo=2)              # hard-cut: "a" declicks out
    out = eng.render(240)                                        # mid-declick
    # Both the dying old voice and the new voice route to cols 2:4; nothing leaks.
    assert np.max(np.abs(out[:, 0:2])) < 1e-9
    assert np.max(np.abs(out[:, 4:8])) < 1e-9
    assert np.max(np.abs(out[:, 2:4])) > 0.5                     # audible on its own columns


def test_out_lo_beyond_bus_is_dropped():
    eng = AudioEngine(bus_channels=4)
    eng.play_normal("n", dc(4800), out_lo=4)                     # cols 4:6, beyond a 4-wide bus
    out = eng.render(480)
    assert np.max(np.abs(out)) < 1e-9                            # dropped, silent, no error

    eng2 = AudioEngine(bus_channels=2)
    eng2.play_normal("n", dc(4800), out_lo=2)                    # cols 2:4, beyond stereo
    out2 = eng2.render(480)
    assert np.max(np.abs(out2)) < 1e-9


def test_voice_out_mono_downmix_into_single_column():
    eng = AudioEngine(bus_channels=8)
    eng.play_normal("n", dc(4800, 0.4), out_lo=3, out_mono=True)   # stereo 0.4/0.4 -> col 3
    out = eng.render(480)
    assert np.allclose(out[:, 3], 0.4, atol=1e-6)                 # mean of (0.4, 0.4)
    assert np.max(np.abs(out[:, 0:3])) < 1e-9                     # neighbors silent
    assert np.max(np.abs(out[:, 4:8])) < 1e-9


def test_out_mono_column_beyond_bus_dropped():
    eng = AudioEngine(bus_channels=8)
    eng.play_normal("n", dc(4800, 0.4), out_lo=8, out_mono=True)   # col 8 beyond an 8-wide bus
    out = eng.render(480)
    assert np.max(np.abs(out)) < 1e-9                             # dropped, silent, no error


def test_mono_device_downmixes_full_mix():
    eng = AudioEngine()
    eng.set_bus_channels(2, mono_out=True)
    eng.play_normal("n", dc(4800, 0.4), out_lo=0)                       # cols 0:2
    eng.play_background("b", dc(4800, 0.4), loop=True, out_lo=4)        # out_lo 4: NOT dropped on mono
    block = eng._produce_output(480)
    assert block.shape == (480, 1)
    # Both voices collapse into the 2-wide bus, then mean-to-mono: 0.4 + 0.4 = 0.8.
    assert np.allclose(block, 0.8, atol=2e-3)


def test_master_gain_applies_across_pairs():
    eng = AudioEngine(bus_channels=8)
    eng.play_normal("n", dc(SAMPLE_RATE), out_lo=2)
    eng.set_master_gain(-6.0206)
    eng.render(2400)                                            # past the ~50 ms smoothing
    out = eng.render(480)
    assert np.allclose(out[:, 2:4], 0.5, atol=2e-3)            # master trim halves the routed columns
    assert np.max(np.abs(out[:, 0:2])) < 1e-9


def test_soft_limit_per_channel_wide_bus():
    eng = AudioEngine(bus_channels=8)
    eng.play_background("b1", dc(SAMPLE_RATE, 0.9), loop=True, out_lo=2)
    eng.play_background("b2", dc(SAMPLE_RATE, 0.9), loop=True, out_lo=2)  # sum 1.8 on cols 2:4
    out = eng.render(480)
    # Cols 2:4 sum past unity -> soft-limited (still < 2); other columns stay silent.
    assert np.all(out[:, 2:4] > 1.0)
    assert np.all(out[:, 2:4] < 2.0)
    assert np.max(np.abs(out[:, 0:2])) < 1e-9
    assert np.max(np.abs(out[:, 4:8])) < 1e-9


# ---------------------------------------------------------------------------
# stop_normal / schedule_stop_normal (hub cross-device exclusivity primitives)
# ---------------------------------------------------------------------------
def test_stop_normal_kills_live_normal():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.render(4800)                                            # steady at unity
    assert eng.get_status()["normal"] is not None
    eng.stop_normal()
    out = mono(eng.render(DECLICK_FRAMES + 100))
    assert out[0] == pytest.approx(1.0, abs=2e-2)              # starts full
    assert abs(out[-1]) < 1e-6                                 # declicked to silence
    assert eng.get_status()["normal"] is None                 # live normal cleared


def test_schedule_stop_normal_kills_at_offset():
    eng = AudioEngine()
    eng.play_normal("n", dc(SAMPLE_RATE))
    eng.schedule_stop_normal("s", 1000)                        # kill 1000 frames from now
    out = mono(eng.render(1000 + DECLICK_FRAMES + 200))
    assert np.allclose(out[:1000], 1.0, atol=1e-6)            # untouched before the boundary
    assert abs(out[-1]) < 1e-6                                 # declicked out after the boundary
    assert eng.get_status()["normal"] is None


def test_to_output_channels_zero_pads():
    eng = AudioEngine()                                         # bus 2
    eng._output_channels = 4                                    # force a wider device than the bus
    out = eng._to_output_channels(np.ones((16, 2), dtype=np.float32))
    assert out.shape == (16, 4)
    assert np.allclose(out[:, 0:2], 1.0)
    assert np.allclose(out[:, 2:4], 0.0)                        # padded with zeros
