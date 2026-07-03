# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Deep unit tests for the ShowController reducer."""

from __future__ import annotations

import pytest

from cueforge.server.controller import ShowController

from .conftest import HASH_B


# ---------------------------------------------------------------------------
# Sequence / snapshot basics
# ---------------------------------------------------------------------------
def test_sequence_is_column_major_gaps_skipped(controller, session):
    t = session.t
    rt = controller.build_runtime()
    assert rt["sequence"] == [t["n1"], t["n2"], t["bg"], t["n3"], t["stop"]]
    assert rt["currentPage"] == t["page"]
    assert rt["cursorIndex"] == 0


# ---------------------------------------------------------------------------
# GO + goLock
# ---------------------------------------------------------------------------
def test_go_fires_cursor_and_advances(controller, session, fake_engine):
    t = session.t
    controller.go()
    assert fake_engine.last()[0] == "play_normal"
    assert fake_engine.last()[1] == t["n1"]  # cue_id == placement id
    assert controller.cursors[t["page"]] == 1


def test_second_go_within_500ms_ignored_then_works(controller, session, fake_engine, clock):
    t = session.t
    controller.go()  # fires n1
    assert controller.cursors[t["page"]] == 1
    clock.advance(0.2)  # still inside 500 ms lock
    controller.go()
    assert controller.cursors[t["page"]] == 1  # ignored
    assert len(fake_engine.calls) == 1
    clock.advance(0.31)  # lock elapsed (0.51 total)
    controller.go()  # fires n2
    assert controller.cursors[t["page"]] == 2
    assert fake_engine.calls[-1][1] == t["n2"]


def test_go_parks_at_end(controller, session, fake_engine, clock):
    t = session.t
    for _ in range(10):
        controller.go()
        clock.advance(0.6)
    assert controller.cursors[t["page"]] == 5  # len(sequence)
    n_calls = len(fake_engine.calls)
    clock.advance(0.6)
    controller.go()  # parked: nothing fires
    assert len(fake_engine.calls) == n_calls


def test_go_lock_remaining_ms(controller, clock):
    controller.go()
    assert controller.go_lock_remaining_ms() == 500
    clock.advance(0.4)
    assert controller.go_lock_remaining_ms() == 100
    clock.advance(0.2)
    assert controller.go_lock_remaining_ms() == 0


# ---------------------------------------------------------------------------
# fire / greens
# ---------------------------------------------------------------------------
def test_fire_moves_cursor_to_index_plus_one(controller, session, fake_engine):
    t = session.t
    controller.fire(t["bg"])  # index 2 -> cursor 3
    assert controller.cursors[t["page"]] == 3
    # greens = sequence[0:cursorIndex]
    rt = controller.build_runtime()
    greens = rt["sequence"][: rt["cursorIndex"]]
    assert greens == [t["n1"], t["n2"], t["bg"]]
    assert fake_engine.last()[0] == "play_background"


# ---------------------------------------------------------------------------
# standby / cursor_move (silent -- no engine calls)
# ---------------------------------------------------------------------------
def test_standby_sets_cursor_without_firing(controller, session, fake_engine):
    t = session.t
    controller.standby(t["n3"])  # index 3
    assert controller.cursors[t["page"]] == 3
    assert fake_engine.calls == []


def test_cursor_move_up_down(controller, session, fake_engine):
    t = session.t
    controller.standby(t["n2"])  # index 1
    controller.cursor_move("down")
    assert controller.cursors[t["page"]] == 2
    controller.cursor_move("up")
    controller.cursor_move("up")
    assert controller.cursors[t["page"]] == 0  # clamped
    assert fake_engine.calls == []


def test_cursor_move_left_right_column_jump(controller, session, fake_engine):
    t = session.t
    # From column A -> right jumps to first cue of column B (index 3).
    controller.cursor_move("right")
    assert controller.cursors[t["page"]] == 3
    # From column B -> left jumps back to first cue of column A (index 0).
    controller.cursor_move("left")
    assert controller.cursors[t["page"]] == 0
    # Clamp at edges.
    controller.cursor_move("left")
    assert controller.cursors[t["page"]] == 0
    assert fake_engine.calls == []


# ---------------------------------------------------------------------------
# reset / panic
# ---------------------------------------------------------------------------
def test_reset_zeros_cursors_and_panics(controller, session, fake_engine, clock):
    t = session.t
    controller.go()
    clock.advance(0.6)
    controller.go()
    assert controller.cursors[t["page"]] == 2
    controller.reset()
    assert controller.cursors[t["page"]] == 0
    assert ("panic", None, {}) in fake_engine.calls


def test_panic_calls_engine(controller, fake_engine):
    controller.panic()
    assert fake_engine.last()[0] == "panic"


# ---------------------------------------------------------------------------
# Firing parameter correctness
# ---------------------------------------------------------------------------
def test_fire_normal_passes_params(controller, session, fake_engine):
    t = session.t
    controller.fire(t["n1"])
    name, cue_id, kw = fake_engine.last()
    assert name == "play_normal"
    assert cue_id == t["n1"]
    assert set(kw) == {"gain_db", "fade_in", "fade_out", "fade_shape"}


def test_fire_background_passes_loop(controller, session, fake_engine):
    t = session.t
    controller.fire(t["bg"])
    name, cue_id, kw = fake_engine.last()
    assert name == "play_background"
    assert kw["loop"] is True
    assert kw["gain_db"] == -3.0
    assert "fade_out" not in kw  # backgrounds have no fade_out param


def test_fire_stop_all_backgrounds(controller, session, fake_engine):
    t = session.t
    controller.fire(t["stop"])
    name, _cid, kw = fake_engine.last()
    assert name == "stop_all_backgrounds"
    assert kw == {"mode": "fade", "fade_seconds": 1.5}


def test_fire_stop_specific_target(controller, session, fake_engine):
    t = session.t
    # Make a stop item targeting the BG library item, and simulate BG running.
    item = session.show.library[t["li_stop"]]
    item.stop_target = t["li_bg"]
    item.stop_mode = "hard"
    fake_engine.status["backgrounds"] = [
        {"cue_id": t["bg"], "frame": 0, "total_frames": 100, "loop": True}
    ]
    controller.fire(t["stop"])
    name, cid, kw = fake_engine.last()
    assert name == "stop_background"
    assert cid == t["bg"]
    assert kw["mode"] == "hard"


# ---------------------------------------------------------------------------
# Edit-mode structural actions
# ---------------------------------------------------------------------------
def test_move_cue_swaps_occupied_cell(controller, session):
    t = session.t
    p_n1 = controller._placement(t["n1"])  # colA row0
    p_n2 = controller._placement(t["n2"])  # colA row1
    controller.move_cue(t["n1"], p_n2.column, p_n2.row)  # onto n2's cell
    assert (p_n1.column, p_n1.row) == (t["colA"], 1)
    assert (p_n2.column, p_n2.row) == (t["colA"], 0)


def test_move_cue_to_empty_cell(controller, session):
    t = session.t
    p_n1 = controller._placement(t["n1"])
    controller.move_cue(t["n1"], t["colB"], 3)  # empty
    assert (p_n1.column, p_n1.row) == (t["colB"], 3)


def test_delete_library_item_removes_item_and_placements(controller, session):
    t = session.t
    controller.delete_library_item(t["li_n1"])
    assert t["li_n1"] not in session.show.library
    assert all(p.library_item_id != t["li_n1"] for p in session.show.placements)


def test_remove_placement(controller, session):
    t = session.t
    controller.remove_placement(t["n1"])
    assert all(p.id != t["n1"] for p in session.show.placements)


def test_remove_placement_stops_its_voice(controller, session, fake_engine):
    """Deleting a placement must stop any audio it is currently playing."""
    t = session.t
    controller.remove_placement(t["n1"])
    assert ("stop_cue", t["n1"], {}) in fake_engine.calls


def test_delete_library_item_stops_playing_placements(controller, session, fake_engine):
    t = session.t
    controller.delete_library_item(t["li_n1"])
    # n1 is a placement of li_n1; its voice must be stopped.
    assert ("stop_cue", t["n1"], {}) in fake_engine.calls


def test_remove_column_drops_its_placements(controller, session):
    t = session.t
    controller.remove_column(t["colA"])
    page = session.show.pages[0]
    assert all(c.id != t["colA"] for c in page.columns)
    assert all(p.column != t["colA"] for p in session.show.placements)


def test_add_and_rename_page(controller, session):
    controller.add_page("Act II")
    names = [p.name for p in session.show.pages]
    assert "Act II" in names
    pid = session.show.pages[-1].id
    controller.rename_page(pid, "Finale")
    assert session.show.pages[-1].name == "Finale"


def test_add_and_rename_column(controller, session):
    t = session.t
    controller.add_column(t["page"], "Act III")
    page = session.show.pages[0]
    assert page.columns[-1].name == "Act III"
    cid = page.columns[-1].id
    controller.rename_column(cid, "Encore")
    assert page.columns[-1].name == "Encore"


def test_set_rows(controller, session):
    t = session.t
    controller.set_rows(t["colA"], 12)
    col = next(c for c in session.show.pages[0].columns if c.id == t["colA"])
    assert col.rows == 12


def test_place_cue(controller, session):
    t = session.t
    before = len(session.show.placements)
    controller.place_cue(t["li_n1"], t["page"], t["colB"], 3)
    assert len(session.show.placements) == before + 1


# ---------------------------------------------------------------------------
# Library actions
# ---------------------------------------------------------------------------
def test_update_library_item_camel_and_snake(controller, session):
    t = session.t
    controller.update_library_item(t["li_n1"], {"gainDb": -6.0, "fade_in": 2.0})
    item = session.show.library[t["li_n1"]]
    assert item.gain_db == -6.0
    assert item.fade_in == 2.0


def test_duplicate_library_item_appends_copy(controller, session):
    t = session.t
    clone = controller.duplicate_library_item(t["li_bg"])
    assert clone is not None
    assert clone.name == "BG (copy)"
    assert clone.audio_hash == HASH_B  # shares audio
    assert clone.id != t["li_bg"]
    assert clone.id in session.show.library


def test_normalize_item_sets_gain(controller, session):
    t = session.t
    controller.normalize_item(t["li_n1"])
    item = session.show.library[t["li_n1"]]
    # Peak of a 0.5 tone -> normalize toward -1 dBFS raises gain above 0.
    assert item.gain_db > 0.0


# ---------------------------------------------------------------------------
# Audition
# ---------------------------------------------------------------------------
def test_audition_item_and_stop(controller, session, fake_engine):
    t = session.t
    controller.audition_item(t["li_bg"])
    assert fake_engine.last()[0] == "audition"
    controller.stop_audition()
    assert fake_engine.last()[0] == "stop_audition"


# ---------------------------------------------------------------------------
# Dispatch mapping
# ---------------------------------------------------------------------------
def test_dispatch_routes_actions(controller, session, fake_engine):
    t = session.t
    controller.dispatch("fire", {"placementId": t["n1"]})
    assert fake_engine.last()[0] == "play_normal"
    controller.dispatch("setEditMode", {"on": True})
    assert controller.edit_mode is True
    with pytest.raises(ValueError):
        controller.dispatch("bogus", {})


# ---------------------------------------------------------------------------
# Snapshot mapping of engine status
# ---------------------------------------------------------------------------
def test_runtime_maps_playing_and_backgrounds(controller, session, fake_engine):
    t = session.t
    fake_engine.status["normal"] = {
        "cue_id": t["n1"], "frame": 10, "total_frames": 200, "finished": False,
    }
    fake_engine.status["backgrounds"] = [
        {"cue_id": t["bg"], "frame": 5, "total_frames": 50, "loop": True}
    ]
    rt = controller.build_runtime(clients=3)
    assert rt["playing"] == {
        "placementId": t["n1"], "cueId": t["n1"], "frame": 10, "totalFrames": 200,
    }
    assert rt["backgrounds"][0]["placementId"] == t["bg"]
    assert rt["backgrounds"][0]["loop"] is True
    assert rt["clients"] == 3
    assert rt["deviceOk"] is True


def test_audition_reports_position_with_library_item(controller, session, fake_engine):
    t = session.t
    controller.audition_item(t["li_n1"])
    assert fake_engine.last()[0] == "audition"

    # Engine now reports the audition voice's position; the controller tags it
    # with the library item id it started so each client can gate the marker.
    fake_engine.status["audition"] = {
        "cue_id": "__audition__", "frame": 1200, "total_frames": 24000, "finished": False,
    }
    fake_engine.status["audition_active"] = True
    rt = controller.build_runtime()
    assert rt["audition"] == {
        "frame": 1200, "totalFrames": 24000, "libraryItemId": t["li_n1"],
    }
    assert rt["auditionActive"] is True


def test_audition_position_none_forgets_item(controller, session, fake_engine):
    t = session.t
    controller.audition_item(t["li_n1"])
    # Engine reports no audition voice (finished / stopped) -> null block and the
    # tracked item id is forgotten.
    fake_engine.status["audition"] = None
    rt = controller.build_runtime()
    assert rt["audition"] is None
    assert controller._audition_item_id is None


def test_stop_audition_clears_tracked_item(controller, session, fake_engine):
    t = session.t
    controller.audition_item(t["li_n1"])
    assert controller._audition_item_id == t["li_n1"]
    controller.stop_audition()
    assert fake_engine.last()[0] == "stop_audition"
    assert controller._audition_item_id is None


def test_no_session_controller_is_inert(fake_engine, clock):
    c = ShowController(fake_engine, None, time_func=clock)
    c.go()
    c.fire("x")
    c.reset()  # panic still fine
    rt = c.build_runtime()
    assert rt["sequence"] == []
    assert rt["currentPage"] is None
