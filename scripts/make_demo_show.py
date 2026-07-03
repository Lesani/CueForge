# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Create the demo .cueforge show used for screenshots and E2E testing.

Synthesizes a small two-act theater show from scratch (numpy -> wav -> the
real import pipeline): music beds, ambience loops, SFX one-shots, and stop
cues, organized into library groups and laid out across two pages. The audio
is generated (chords, filtered noise, chirps) so the repo ships no third-party
recordings, but durations and waveforms look like a real show.

Run: .venv/Scripts/python.exe scripts/make_demo_show.py
Writes: ~/CueForge/projects/Demo.cueforge (open it by name in the UI).
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import soundfile as sf

from cueforge import project as P
from cueforge.audio_format import SAMPLE_RATE

rng = np.random.default_rng(42)  # deterministic output


# --------------------------------------------------------------------------
# Synthesis helpers (mono float32 -> stereo written by write_wav)
# --------------------------------------------------------------------------
def _env(n: int, attack: float, release: float) -> np.ndarray:
    """Linear attack / exponential-ish release envelope, length n samples."""
    e = np.ones(n, dtype=np.float32)
    a = min(n, max(1, int(attack * SAMPLE_RATE)))
    e[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    r = min(n, max(1, int(release * SAMPLE_RATE)))
    e[n - r:] *= np.linspace(1.0, 0.0, r, dtype=np.float32) ** 1.5
    return e

def _t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * SAMPLE_RATE), dtype=np.float32) / SAMPLE_RATE

def _norm(x: np.ndarray, peak: float = 0.5) -> np.ndarray:
    m = float(np.max(np.abs(x))) or 1.0
    return (x * (peak / m)).astype(np.float32)

def _smooth(x: np.ndarray, width: int) -> np.ndarray:
    """Cheap low-pass: moving average (kills noise fizz for wind/crowd)."""
    k = np.ones(width, dtype=np.float32) / width
    return np.convolve(x, k, mode="same").astype(np.float32)

def bell(freqs: list[float], seconds: float, strike_decay: float = 1.5) -> np.ndarray:
    """A bell/chime strike: inharmonic partials with exponential decay."""
    t = _t(seconds)
    out = np.zeros_like(t)
    for i, f in enumerate(freqs):
        decay = np.exp(-t * (strike_decay + i * 0.8))
        out += np.sin(2 * np.pi * f * t) * decay / (i + 1)
    return out

def chord_pad(freq_sets: list[list[float]], each: float, fade: float = 0.4) -> np.ndarray:
    """A slow pad: a sequence of soft chords (for music beds/themes)."""
    parts = []
    for freqs in freq_sets:
        t = _t(each)
        c = np.zeros_like(t)
        for f in freqs:
            c += np.sin(2 * np.pi * f * t)
            c += 0.35 * np.sin(2 * np.pi * f * 2 * t)   # soft octave shimmer
        parts.append(c * _env(len(t), fade, fade))
    return np.concatenate(parts)

def melody(notes: list[tuple[float, float]]) -> np.ndarray:
    """Plucked melody: (freq, seconds) notes with a percussive decay."""
    parts = []
    for f, secs in notes:
        t = _t(secs)
        tone = np.sin(2 * np.pi * f * t) + 0.4 * np.sin(2 * np.pi * f * 2 * t)
        parts.append(tone * np.exp(-t * 3.0))
    return np.concatenate(parts)

def noise_bed(seconds: float, smooth: int, lfo_hz: float = 0.1) -> np.ndarray:
    """Filtered noise with a slow swell LFO (rain / wind / crowd base)."""
    x = rng.standard_normal(int(seconds * SAMPLE_RATE)).astype(np.float32)
    x = _smooth(x, smooth)
    t = _t(seconds)
    lfo = 0.75 + 0.25 * np.sin(2 * np.pi * lfo_hz * t + rng.uniform(0, 6.28))
    return x * lfo * _env(len(x), 1.0, 1.0)

def rumble(seconds: float, sharp: bool = True) -> np.ndarray:
    """Low rumble (thunder): integrated noise with a hard attack."""
    x = np.cumsum(rng.standard_normal(int(seconds * SAMPLE_RATE))).astype(np.float32)
    x -= _smooth(x, 4800)                       # remove drift, keep low rumble
    t = _t(seconds)
    attack = 0.005 if sharp else 0.8
    return x * _env(len(x), attack, seconds * 0.7) * np.exp(-t * 0.8)

def crickets(seconds: float) -> np.ndarray:
    """Night crickets: 4.2 kHz chirp bursts repeating at ~1.4 Hz."""
    t = _t(seconds)
    carrier = np.sin(2 * np.pi * 4200 * t)
    chirp = (np.sin(2 * np.pi * 32 * t) > 0.4).astype(np.float32)   # trill
    burst = (np.sin(2 * np.pi * 1.4 * t) > 0.15).astype(np.float32)  # phrase
    return carrier * chirp * burst * 0.8 + rng.standard_normal(len(t)).astype(np.float32) * 0.01

def applause(seconds: float) -> np.ndarray:
    """Crowd applause: dense random claps swelling in, thinning out."""
    n = int(seconds * SAMPLE_RATE)
    out = np.zeros(n, dtype=np.float32)
    density = np.linspace(1.0, 0.25, n)
    claps = rng.random(n) < (0.002 * density)
    idx = np.flatnonzero(claps)
    clap = rng.standard_normal(360).astype(np.float32) * np.exp(-np.arange(360) / 60)
    for i in idx:
        end = min(n, i + len(clap))
        out[i:end] += clap[: end - i] * rng.uniform(0.4, 1.0)
    return out * _env(n, 0.6, seconds * 0.4)


# --------------------------------------------------------------------------
# The show
# --------------------------------------------------------------------------
A3, C4, D4, E4, F4, G4, A4, B4, C5, D5, E5, G5 = (
    220.0, 261.6, 293.7, 329.6, 349.2, 392.0, 440.0, 493.9, 523.3, 587.3, 659.3, 784.0
)

def build_audio(tmp: str) -> dict[str, str]:
    """Render every cue's audio to a wav in ``tmp``; returns name -> path."""
    specs: dict[str, np.ndarray] = {
        # Music
        "House Music": chord_pad([[A3, C4, E4], [F4 / 2, A3, C4], [G4 / 2, B4 / 2, D4], [A3, C4, E4]], 4.0),
        "Opening Theme": np.concatenate([
            melody([(E4, 0.5), (G4, 0.5), (A4, 0.75), (G4, 0.25), (E4, 0.5), (D4, 0.5), (C4, 1.0)]),
            chord_pad([[C4, E4, G4], [A3, C4, E4], [F4, A4, C5], [G4, B4, D5], [C4, E4, G4, C5]], 3.2),
        ]),
        "Finale Theme": np.concatenate([
            chord_pad([[C4, E4, G4], [G4, B4, D5], [A4, C5, E5], [F4, A4, C5]], 3.0),
            melody([(C5, 0.4), (D5, 0.4), (E5, 0.8), (G5, 1.2), (E5, 0.6), (C5, 1.6)]),
            chord_pad([[C4, E4, G4, C5]], 4.0),
        ]),
        "Curtain Call": chord_pad([[F4, A4, C5], [G4, B4, D5], [C4, E4, G4], [C4, F4, A4], [C4, E4, G4, C5]], 3.6),
        # Ambience beds
        "Crowd Walla": noise_bed(28.0, smooth=96, lfo_hz=0.23) + noise_bed(28.0, smooth=32, lfo_hz=0.11) * 0.4,
        "Wind Rising": noise_bed(34.0, smooth=220, lfo_hz=0.07) * np.linspace(0.35, 1.0, int(34.0 * SAMPLE_RATE)),
        "Rain Bed": noise_bed(30.0, smooth=6, lfo_hz=0.15),
        "Crickets": crickets(26.0),
        "Birdsong": np.concatenate([bell([2800, 3400], 0.35, 6.0) * 0.6, np.zeros(int(0.5 * SAMPLE_RATE), np.float32),
                                    bell([3100, 3900], 0.3, 7.0) * 0.5, np.zeros(int(0.8 * SAMPLE_RATE), np.float32),
                                    bell([2600, 3200], 0.4, 6.0) * 0.55] * 6),
        # SFX
        "Welcome Chime": bell([660, 880, 1320], 4.0, 1.2),
        "Church Bells": np.concatenate([bell([392, 587, 784, 1175], 2.6, 0.9) for _ in range(3)]),
        "Dog Bark": np.concatenate([rumble(0.35) * 0.8, np.zeros(int(0.4 * SAMPLE_RATE), np.float32),
                                    rumble(0.3) * 0.9, np.zeros(int(0.5 * SAMPLE_RATE), np.float32),
                                    rumble(0.4)]),
        "Thunder Crack": rumble(5.0, sharp=True),
        "Lightning Strike": rumble(1.6, sharp=True) * 1.2,
        "Owl Call": np.concatenate([bell([310, 465], 0.9, 2.2), np.zeros(int(0.4 * SAMPLE_RATE), np.float32), bell([295, 440], 1.1, 2.0)]),
        "Midnight Clock": np.concatenate([np.concatenate([bell([220, 440, 660], 0.95, 1.4), np.zeros(int(0.05 * SAMPLE_RATE), np.float32)]) for _ in range(12)]),
        "Applause": applause(12.0),
    }
    paths = {}
    for name, mono in specs.items():
        mono = _norm(mono)
        # slight stereo width: right channel delayed by 12 samples
        right = np.roll(mono, 12)
        wav = os.path.join(tmp, f"{name}.wav")
        sf.write(wav, np.column_stack([mono, right]), SAMPLE_RATE)
        paths[name] = wav
    return paths


def main() -> None:
    home = os.path.expanduser("~")
    projects_dir = os.path.join(home, "CueForge", "projects")
    work_dir = os.path.join(home, "CueForge", "work", "Demo")
    os.makedirs(projects_dir, exist_ok=True)
    os.makedirs(work_dir, exist_ok=True)

    session = P.ProjectSession.create_new(work_dir, "Demo")
    show = session.show

    with tempfile.TemporaryDirectory() as tmp:
        paths = build_audio(tmp)
        items = {}
        for name, wav in paths.items():
            items[name] = P.import_audio(session, wav, name=name).item

    def style(name, *, group="", type=None, loop=False, fade_in=0.0, fade_out=0.0):
        it = items[name]
        it.group = group
        if type:
            it.type = type
        it.loop = loop
        it.fade_in = fade_in
        it.fade_out = fade_out
        return it

    # Music
    style("House Music", group="Music", type="background", loop=True, fade_in=2.0)
    style("Opening Theme", group="Music", fade_out=1.5)
    style("Finale Theme", group="Music", fade_out=2.0)
    style("Curtain Call", group="Music")
    # Ambience (background beds)
    style("Crowd Walla", group="Ambience", type="background", loop=True, fade_in=1.5)
    style("Wind Rising", group="Ambience", type="background", fade_in=3.0)
    style("Rain Bed", group="Ambience", type="background", loop=True, fade_in=2.0)
    style("Crickets", group="Ambience", type="background", loop=True, fade_in=1.0)
    style("Birdsong", group="Ambience", type="background", loop=True, fade_in=1.0)
    # SFX
    for sfx in ("Welcome Chime", "Church Bells", "Dog Bark", "Thunder Crack",
                "Lightning Strike", "Owl Call", "Midnight Clock", "Applause"):
        style(sfx, group="SFX")

    def stop_cue(name, *, mode="fade", fade=2.0):
        it = P.make_library_item(name, type="stop")
        it.group = "Control"
        it.stop_target = "allBackgrounds"
        it.stop_mode = mode
        it.stop_fade_seconds = fade
        show.library[it.id] = it
        return it

    stop_ambience = stop_cue("Stop Ambience", fade=2.0)
    stop_storm = stop_cue("Stop Storm", mode="hard")
    stop_all = stop_cue("Stop Everything", fade=3.0)

    # ---- Pages ------------------------------------------------------------
    def place(page, col, layout):
        for row, item in enumerate(layout):
            if item is None:
                continue
            show.placements.append(P.make_placement(item.id, page.id, col.id, row))

    c1 = P.make_column("Preshow", rows=6)
    c2 = P.make_column("Scene 1 - Market", rows=6)
    c3 = P.make_column("Scene 2 - Storm", rows=6)
    act1 = P.make_page("Act I", columns=[c1, c2, c3])
    show.pages.append(act1)
    place(act1, c1, [items["House Music"], items["Welcome Chime"], items["Opening Theme"]])
    place(act1, c2, [items["Birdsong"], items["Crowd Walla"], items["Church Bells"],
                     items["Dog Bark"], stop_ambience])
    place(act1, c3, [items["Wind Rising"], items["Rain Bed"], items["Thunder Crack"],
                     items["Lightning Strike"], stop_storm])

    c4 = P.make_column("Scene 3 - Night", rows=6)
    c5 = P.make_column("Finale", rows=6)
    act2 = P.make_page("Act II", columns=[c4, c5])
    show.pages.append(act2)
    place(act2, c4, [items["Crickets"], items["Owl Call"], items["Midnight Clock"]])
    place(act2, c5, [items["Finale Theme"], items["Applause"], items["Curtain Call"], stop_all])

    session.autosave()
    out = os.path.join(projects_dir, "Demo.cueforge")
    session.save_as(out)
    print(f"[OK] wrote {out}")
    print(f"[OK] library items: {len(show.library)}, placements: {len(show.placements)}")


if __name__ == "__main__":
    main()
