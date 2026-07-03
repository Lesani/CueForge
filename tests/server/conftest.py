# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Fixtures for server (reducer) tests.

A ``FakeEngine`` records every control call and exposes a controllable
``get_status()`` dict, so ``ShowController`` can be exercised WITHOUT FastAPI or a
real audio device. A helper builds a real ``ProjectSession`` on disk with FLAC
audio written directly (no ffmpeg), so ``load_cue_pcm`` works for firing tests.
"""

from __future__ import annotations

import itertools
import os

import numpy as np
import pytest
import soundfile as sf

from cueforge.audio_format import SAMPLE_RATE
from cueforge.project import ProjectSession
from cueforge.project.model import (
    make_column,
    make_library_item,
    make_page,
    make_placement,
)


class FakeEngine:
    """Records control calls; ``get_status`` returns a mutable dict."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.status = {"normal": None, "backgrounds": [], "device_ok": True}

    # -- control API (records name + args) --
    def play_normal(self, cue_id, pcm, **kw):
        self.calls.append(("play_normal", cue_id, kw))

    def play_background(self, cue_id, pcm, **kw):
        self.calls.append(("play_background", cue_id, kw))

    def stop_background(self, cue_id, **kw):
        self.calls.append(("stop_background", cue_id, kw))

    def stop_all_backgrounds(self, **kw):
        self.calls.append(("stop_all_backgrounds", None, kw))

    def panic(self):
        self.calls.append(("panic", None, {}))

    def audition(self, pcm, **kw):
        self.calls.append(("audition", None, kw))

    def stop_audition(self):
        self.calls.append(("stop_audition", None, {}))

    def stop_cue(self, cue_id):
        self.calls.append(("stop_cue", cue_id, {}))

    def get_status(self):
        return self.status

    # -- test helpers --
    def names(self):
        return [c[0] for c in self.calls]

    def last(self):
        return self.calls[-1] if self.calls else None


class Clock:
    """Manually advanced monotonic clock for GO-lock tests."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def fake_engine():
    return FakeEngine()


@pytest.fixture
def clock():
    return Clock()


# Valid (all-hex) content hashes -- audio_path rejects anything else.
HASH_N = "1" * 64
HASH_B = "2" * 64


def _write_flac(session: ProjectSession, audio_hash: str, seconds: float = 0.5) -> None:
    n = int(seconds * SAMPLE_RATE)
    tone = 0.5 * np.sin(2 * np.pi * 440 * np.arange(n) / SAMPLE_RATE)
    stereo = np.stack([tone, tone], axis=1).astype(np.float32)
    sf.write(session.audio_path(audio_hash), stereo, SAMPLE_RATE, format="FLAC")


@pytest.fixture
def session(tmp_path):
    """A real ProjectSession with a small deterministic grid + audio.

    Layout (page "P1"):
        Column A (rows 4)   Column B (rows 4)
          A0 -> pn1 (normal)  B0 -> pn3 (normal)
          A1 -> pn2 (normal)  B1 -> (empty gap)
          A2 -> pbg (background loop)  B2 -> pstop (stop allBackgrounds)
    GO sequence (column-major, gaps skipped): pn1, pn2, pbg, pn3, pstop
    """
    ids = (f"id{i}" for i in itertools.count())
    factory = lambda: next(ids)  # noqa: E731

    sess = ProjectSession.create_new(str(tmp_path / "work"), "TestShow")
    show = sess.show

    # Audio blobs.
    _write_flac(sess, HASH_N)
    _write_flac(sess, HASH_B)

    # Library items.
    li_n1 = make_library_item("Normal 1", type="normal", audio_hash=HASH_N, id_factory=factory)
    li_n2 = make_library_item("Normal 2", type="normal", audio_hash=HASH_N, id_factory=factory)
    li_n3 = make_library_item("Normal 3", type="normal", audio_hash=HASH_N, id_factory=factory)
    li_bg = make_library_item(
        "BG", type="background", audio_hash=HASH_B, loop=True, gain_db=-3.0, id_factory=factory
    )
    li_stop = make_library_item(
        "StopAll", type="stop", id_factory=factory,
        stop_target="allBackgrounds", stop_mode="fade", stop_fade_seconds=1.5,
    )
    for li in (li_n1, li_n2, li_n3, li_bg, li_stop):
        show.library[li.id] = li

    # Grid: one page, two columns.
    col_a = make_column("A", 4, id_factory=factory)
    col_b = make_column("B", 4, id_factory=factory)
    page = make_page("P1", [col_a, col_b], id_factory=factory)
    show.pages.append(page)

    # Placements (placement id used as engine cue_id).
    p_n1 = make_placement(li_n1.id, page.id, col_a.id, 0, id_factory=factory)
    p_n2 = make_placement(li_n2.id, page.id, col_a.id, 1, id_factory=factory)
    p_bg = make_placement(li_bg.id, page.id, col_a.id, 2, id_factory=factory)
    p_n3 = make_placement(li_n3.id, page.id, col_b.id, 0, id_factory=factory)
    p_stop = make_placement(li_stop.id, page.id, col_b.id, 2, id_factory=factory)
    show.placements.extend([p_n1, p_n2, p_bg, p_n3, p_stop])
    sess.autosave()

    # Expose handy references on the session object for tests.
    sess.t = {  # type: ignore[attr-defined]
        "page": page.id,
        "colA": col_a.id,
        "colB": col_b.id,
        "n1": p_n1.id, "n2": p_n2.id, "bg": p_bg.id, "n3": p_n3.id, "stop": p_stop.id,
        "li_n1": li_n1.id, "li_bg": li_bg.id, "li_stop": li_stop.id,
    }
    return sess


@pytest.fixture
def controller(fake_engine, clock, session):
    from cueforge.server.controller import ShowController

    return ShowController(fake_engine, session, time_func=clock)
