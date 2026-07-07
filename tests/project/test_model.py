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
        type="normal",
        background=True,
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


def test_background_flag_roundtrip():
    # The role flag survives to_dict/from_dict on a normal sound.
    item = m.make_library_item("Amb", type="normal", audio_hash="h", background=True)
    d = item.to_dict()
    assert d["background"] is True
    assert m.LibraryItem.from_dict(d).background is True
    # And a compound can carry it too.
    comp = m.make_library_item("Loop", type="compound", background=True)
    assert m.LibraryItem.from_dict(comp.to_dict()).background is True


def test_background_defaults_false():
    item = m.make_library_item("Plain", type="normal", audio_hash="h")
    assert item.background is False
    assert item.to_dict()["background"] is False
    # A show saved before ADR 0006 carries no "background" key -> defaults False.
    loaded = m.LibraryItem.from_dict({"id": "x", "name": "Old", "type": "normal"})
    assert loaded.background is False


def test_legacy_background_type_migrates_to_role_flag():
    # ADR 0006: the old type=="background" becomes type="normal" + background=True.
    legacy = {"id": "x", "name": "Old BG", "type": "background",
              "audioHash": "h", "loop": True}
    item = m.LibraryItem.from_dict(legacy)
    assert item.type == "normal"
    assert item.background is True
    assert item.loop is True  # loop preserved
    # And it is no longer a "background" type on save.
    assert item.to_dict()["type"] == "normal"


def test_from_dict_forces_background_false_for_stop_and_fade():
    # Dirty data: a stop/fade dict carrying a stray background flag is coerced off.
    stop = m.LibraryItem.from_dict(
        {"id": "s", "name": "Stop", "type": "stop", "background": True}
    )
    assert stop.background is False
    fade = m.LibraryItem.from_dict(
        {"id": "f", "name": "Fade", "type": "fade", "background": True}
    )
    assert fade.background is False


def test_stop_item_has_null_audio():
    item = m.make_library_item("Stop All", type="stop")
    assert item.audio_hash is None
    assert m.LibraryItem.from_dict(item.to_dict()).audio_hash is None


def test_library_item_fade_roundtrip():
    item = m.LibraryItem(
        id="fade1",
        name="Fade backgrounds",
        type="fade",
        fade_target="bgItemId",
        fade_to_db=-9.0,
        fade_time_seconds=4.5,
        fade_stop_when_done=True,
        fade_shape="equalPower",
    )
    d = item.to_dict()
    assert d["fadeTarget"] == "bgItemId"
    assert d["fadeToDb"] == -9.0
    assert d["fadeTimeSeconds"] == 4.5
    assert d["fadeStopWhenDone"] is True
    assert m.LibraryItem.from_dict(d) == item


def test_library_item_loads_without_fade_fields():
    # A show saved before P2 carries no fade keys -> defaults fill in.
    item = m.LibraryItem.from_dict({"id": "x", "name": "Old", "type": "normal"})
    assert item.fade_target == "allBackgrounds"
    assert item.fade_to_db == 0.0
    assert item.fade_time_seconds == 3.0
    assert item.fade_stop_when_done is False


def test_library_item_output_id_roundtrip():
    item = m.make_library_item("Music", type="normal", audio_hash="h1", output_id="out-3")
    d = item.to_dict()
    assert d["outputId"] == "out-3"
    assert m.LibraryItem.from_dict(d) == item
    assert m.LibraryItem.from_dict(d).output_id == "out-3"


def test_from_dict_ignores_output_pair():
    # A show saved before F1 carries a legacy outputPair key -> ignored, no error;
    # output_id defaults to None (the Default Output).
    item = m.LibraryItem.from_dict(
        {"id": "x", "name": "Old", "type": "normal", "outputPair": 3}
    )
    assert item.output_id is None


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


# ---------------------------------------------------------------------------
# Placement sequencing fields (trigger mode + pre-wait)
# ---------------------------------------------------------------------------
def test_placement_roundtrip_preserves_trigger_mode_and_pre_wait():
    p = m.CuePlacement(
        id="p1",
        library_item_id="li1",
        page="pg",
        column="col",
        row=2,
        trigger_mode="afterPrevious",
        pre_wait=0.5,
    )
    d = p.to_dict()
    assert d["triggerMode"] == "afterPrevious"
    assert d["preWait"] == 0.5
    assert m.CuePlacement.from_dict(d) == p


def test_placement_from_dict_defaults_to_on_trigger():
    # An old show's placement carries no sequencing keys.
    d = {"id": "p1", "libraryItemId": "li1", "page": "pg", "column": "col", "row": 0}
    p = m.CuePlacement.from_dict(d)
    assert p.trigger_mode == "onTrigger"
    assert p.pre_wait == 0.0


def test_placement_output_id_roundtrip():
    p = m.CuePlacement(
        id="p1",
        library_item_id="li1",
        page="pg",
        column="col",
        row=2,
        output_id="out-9",
    )
    d = p.to_dict()
    assert d["outputId"] == "out-9"
    assert m.CuePlacement.from_dict(d) == p


def test_placement_from_dict_default_output_id_none():
    d = {"id": "p1", "libraryItemId": "li1", "page": "pg", "column": "col", "row": 0}
    p = m.CuePlacement.from_dict(d)
    assert p.output_id is None


def test_library_item_drops_removed_reserved_fields():
    item = m.make_library_item("Tone", audio_hash="h1")
    d = item.to_dict()
    assert "autoContinue" not in d
    assert "preWait" not in d
    # A dict that still carries the removed keys loads without error (extra keys
    # ignored by name-based from_dict).
    d_legacy = {**d, "autoContinue": {"x": 1}, "preWait": 1.5}
    assert m.LibraryItem.from_dict(d_legacy) == item


def test_make_placement_accepts_trigger_fields():
    p = m.make_placement(
        "li1", "pg", "col", 0, trigger_mode="withPrevious", pre_wait=0.25
    )
    assert p.trigger_mode == "withPrevious"
    assert p.pre_wait == 0.25


# ---------------------------------------------------------------------------
# Compound-cue fields (timeline + render state)
# ---------------------------------------------------------------------------
def test_compound_item_roundtrip():
    timeline = {
        "tracks": [
            {
                "id": "t1",
                "name": "Track 1",
                "gainDb": -2.0,
                "mute": False,
                "clips": [
                    {
                        "id": "c1",
                        "itemId": "src1",
                        "start": 0.5,
                        "clipIn": 0.1,
                        "clipOut": 1.0,
                        "gainDb": 0.0,
                        "fadeIn": 0.2,
                        "fadeOut": 0.3,
                        "fadeShape": "linear",
                        "effects": [],
                    }
                ],
            }
        ]
    }
    item = m.make_library_item(
        "My Compound",
        type="compound",
        timeline=timeline,
        render_signature="sig123",
        render_state="ready",
        render_error="",
    )
    d = item.to_dict()
    assert d["timeline"] == timeline
    assert d["renderSignature"] == "sig123"
    assert d["renderState"] == "ready"
    assert m.LibraryItem.from_dict(d) == item


def test_compound_defaults_tolerant():
    item = m.LibraryItem.from_dict({"id": "x", "name": "Old", "type": "compound"})
    assert item.timeline is None
    assert item.render_signature == ""
    assert item.render_state == ""
    assert item.render_error == ""
