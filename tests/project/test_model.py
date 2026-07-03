# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Model round-trip and deterministic id-factory tests."""

from __future__ import annotations

import itertools

import pytest

from cueforge.project import model as m


def test_library_item_roundtrip():
    item = m.LibraryItem(
        id="abc",
        name="Doorbell",
        type="background",
        audio_hash="deadbeef",
        trim_in=0.25,
        trim_out=0.75,
        gain_db=-3.0,
        fade_in=0.5,
        fade_out=1.0,
        fade_shape="equalPower",
        loop=True,
        stop_target="allBackgrounds",
        stop_mode="fade",
        stop_fade_seconds=3.0,
    )
    d = item.to_dict()
    assert d["audioHash"] == "deadbeef"
    assert m.LibraryItem.from_dict(d) == item


def test_stop_item_has_null_audio():
    item = m.make_library_item("Stop All", type="stop")
    assert item.audio_hash is None
    assert m.LibraryItem.from_dict(item.to_dict()).audio_hash is None


def test_deterministic_id_factory():
    counter = itertools.count()
    ids = lambda: f"id-{next(counter)}"
    a = m.make_library_item("a", id_factory=ids)
    b = m.make_column("Act I", 4, id_factory=ids)
    assert a.id == "id-0"
    assert b.id == "id-1"


def test_show_tree_roundtrip():
    ids = (f"id-{i}" for i in itertools.count())
    fac = lambda: next(ids)
    show = m.make_show("MyShow", id_factory=fac)
    col = m.make_column("Act I", 3, id_factory=fac)
    page = m.make_page("Page 1", [col], id_factory=fac)
    show.pages.append(page)
    item = m.make_library_item("Tone", audio_hash="h1", id_factory=fac)
    show.library[item.id] = item
    show.placements.append(
        m.make_placement(item.id, page.id, col.id, 0, id_factory=fac)
    )
    show.settings["masterTrim"] = -2.0

    d = show.to_dict()
    rebuilt = m.Show.from_dict(d)
    assert rebuilt.to_dict() == d
    assert rebuilt == show


def test_show_carries_format_version():
    show = m.make_show("S")
    assert show.to_dict()["formatVersion"] == m.FORMAT_VERSION


def test_show_without_format_version_is_v1():
    d = m.make_show("S").to_dict()
    del d["formatVersion"]  # pre-versioning file
    rebuilt = m.Show.from_dict(d)
    assert rebuilt.format_version == m.FORMAT_VERSION


def test_show_from_newer_format_rejected():
    d = m.make_show("S").to_dict()
    d["formatVersion"] = m.FORMAT_VERSION + 1
    with pytest.raises(ValueError, match="update CueForge"):
        m.Show.from_dict(d)
