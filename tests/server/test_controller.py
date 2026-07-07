# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Deep unit tests for the ShowController reducer."""

from __future__ import annotations

import numpy as np
import pytest

from cueforge.audio_format import seconds_to_frames
from cueforge.engine.audio_engine import ALL_BACKGROUNDS
from cueforge.server.controller import LIVE_GAIN_RAMP_SECONDS, ShowController

from .conftest import HASH_B


def _placements_named(fake_engine, name):
    return [c for c in fake_engine.calls if c[0] == name]


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
    controller.go()  # fires n1 (a lone onTrigger cue: cancel_scheduled + play_normal)
    assert controller.cursors[t["page"]] == 1
    fires = [c for c in fake_engine.calls if c[0] == "play_normal"]
    assert len(fires) == 1
    clock.advance(0.2)  # still inside 500 ms lock
    controller.go()
    assert controller.cursors[t["page"]] == 1  # ignored: no new fire
    assert [c for c in fake_engine.calls if c[0] == "play_normal"] == fires
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
    fake_engine.calls.clear()  # drop the set_outputs push done at session attach
    controller.standby(t["n3"])  # index 3
    assert controller.cursors[t["page"]] == 3
    assert fake_engine.calls == []


def test_cursor_move_up_down(controller, session, fake_engine):
    t = session.t
    fake_engine.calls.clear()  # drop the set_outputs push done at session attach
    controller.standby(t["n2"])  # index 1
    controller.cursor_move("down")
    assert controller.cursors[t["page"]] == 2
    controller.cursor_move("up")
    controller.cursor_move("up")
    assert controller.cursors[t["page"]] == 0  # clamped
    assert fake_engine.calls == []


def test_cursor_move_left_right_column_jump(controller, session, fake_engine):
    t = session.t
    fake_engine.calls.clear()  # drop the set_outputs push done at session attach
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
    assert set(kw) == {"gain_db", "fade_in", "fade_out", "fade_shape", "output_id"}


def test_fire_background_passes_loop(controller, session, fake_engine):
    t = session.t
    controller.fire(t["bg"])
    name, cue_id, kw = fake_engine.last()
    assert name == "play_background"
    assert kw["loop"] is True
    assert kw["gain_db"] == -3.0
    assert kw["output_id"] is None  # default routing
    assert "fade_out" not in kw  # backgrounds have no fade_out param


def test_fire_normal_passes_output_id(controller, session, fake_engine):
    t = session.t
    session.show.library[t["li_n1"]].output_id = "o1"
    controller.fire(t["n1"])
    name, _cid, kw = fake_engine.last()
    assert name == "play_normal"
    assert kw["output_id"] == "o1"


def test_schedule_passes_output_id(controller, session, fake_engine):
    t = session.t
    n2 = controller._placement(t["n2"])
    li = session.show.library[n2.library_item_id]
    li.output_id = "o2"
    n2.trigger_mode = "withPrevious"
    n2.pre_wait = 0.5                       # positive offset -> scheduled, not fired live
    controller.fire(t["n1"])               # launches the chain n1 -> n2
    sched = [c for c in fake_engine.calls if c[0] == "schedule_normal"]
    assert sched
    assert sched[-1][2]["output_id"] == "o2"


def test_audition_passes_output_id(controller, session, fake_engine):
    t = session.t
    session.show.library[t["li_n1"]].output_id = "o2"
    controller.audition_item(t["li_n1"])
    name, _cid, kw = fake_engine.last()
    assert name == "audition"
    assert kw["output_id"] == "o2"


# ---------------------------------------------------------------------------
# Routing precedence (placement override > item default > Default Output)
# ---------------------------------------------------------------------------
def test_effective_output_precedence_placement_over_item(controller, session):
    t = session.t
    item = session.show.library[t["li_n1"]]
    item.output_id = "itemOut"
    placement = controller._placement(t["n1"])
    placement.output_id = "placementOut"
    assert controller._effective_output_id(placement, item) == "placementOut"


def test_effective_output_inherits_item_when_placement_none(controller, session):
    t = session.t
    item = session.show.library[t["li_n1"]]
    item.output_id = "itemOut"
    placement = controller._placement(t["n1"])
    placement.output_id = None
    assert controller._effective_output_id(placement, item) == "itemOut"


def test_effective_output_default_when_both_none(controller, session):
    t = session.t
    item = session.show.library[t["li_n1"]]
    item.output_id = None
    placement = controller._placement(t["n1"])
    placement.output_id = None
    assert controller._effective_output_id(placement, item) is None


def test_update_output_id(controller, session):
    t = session.t
    li = session.show.library[t["li_n1"]]
    controller.update_library_item(t["li_n1"], {"outputId": "o1"})
    assert li.output_id == "o1"
    # Snake_case wire name is also accepted.
    controller.update_library_item(t["li_n1"], {"output_id": "o2"})
    assert li.output_id == "o2"
    # Empty / falsy -> Default Output (None).
    controller.update_library_item(t["li_n1"], {"outputId": ""})
    assert li.output_id is None


def test_update_placement_output_id(controller, session):
    t = session.t
    placement = controller._placement(t["n1"])
    controller.update_placement(t["n1"], {"outputId": "o1"})
    assert placement.output_id == "o1"
    controller.update_placement(t["n1"], {"outputId": ""})   # falsy -> inherit item
    assert placement.output_id is None


# ---------------------------------------------------------------------------
# Named outputs: set_outputs validation, test tone, session push
# ---------------------------------------------------------------------------
def test_set_outputs_validation(controller, session, fake_engine):
    controller.set_outputs([
        {"id": "o1", "name": "Main L", "device": "Dev A", "channel": 3, "mono": True},
        {"id": "", "name": "no id", "device": "Dev A", "channel": 1},       # dropped: no id
        {"id": "o2", "name": "  ", "device": "Dev A", "channel": 1},        # dropped: blank name
        {"id": "o1", "name": "dup", "device": "Dev A", "channel": 1},       # dropped: dup id
        {"id": "o3", "name": "Coerce", "device": None, "channel": 0},       # channel floored to 1
    ])
    stored = session.show.settings["outputs"]
    assert [o["id"] for o in stored] == ["o1", "o3"]
    assert stored[0] == {
        "id": "o1", "name": "Main L", "device": "Dev A", "channel": 3, "mono": True,
    }
    assert stored[1]["channel"] == 1          # floored
    assert stored[1]["device"] is None
    assert stored[1]["mono"] is False         # default
    # Engine was reconfigured with the validated list.
    set_calls = [c for c in fake_engine.calls if c[0] == "set_outputs"]
    assert set_calls
    assert set_calls[-1][2]["outputs"] == stored


def test_test_output_stereo_and_mono_tone(controller, session, fake_engine):
    session.show.settings["outputs"] = [
        {"id": "st", "name": "Stereo", "device": "Dev A", "channel": 1, "mono": False},
        {"id": "mo", "name": "Mono", "device": "Dev A", "channel": 3, "mono": True},
    ]
    controller.test_output("st")
    name, _cid, kw = fake_engine.last()
    assert name == "audition"
    assert kw["output_id"] == "st"

    controller.test_output("mo")
    # The mono tone is L == R (a single centered beep the engine downmixes).
    from cueforge.engine.tones import make_identification_tone
    mono_tone = make_identification_tone(True)
    assert mono_tone.shape[1] == 2
    assert np.allclose(mono_tone[:, 0], mono_tone[:, 1])
    stereo_tone = make_identification_tone(False)
    assert stereo_tone.shape[1] == 2


def test_set_session_pushes_outputs_to_engine(fake_engine, clock, session):
    session.show.settings["outputs"] = [
        {"id": "o1", "name": "Main", "device": "Dev A", "channel": 1, "mono": False},
    ]
    ShowController(fake_engine, session, time_func=clock)
    set_calls = [c for c in fake_engine.calls if c[0] == "set_outputs"]
    assert set_calls
    assert set_calls[-1][2]["outputs"] == session.show.settings["outputs"]


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


def test_update_library_item_accepts_background_flag(controller, session):
    # ADR 0006: the role flag is settable on a normal sound and coerced to bool.
    t = session.t
    controller.update_library_item(t["li_n1"], {"background": 1})
    assert session.show.library[t["li_n1"]].background is True
    controller.update_library_item(t["li_n1"], {"background": 0})
    assert session.show.library[t["li_n1"]].background is False


def test_update_library_item_forces_background_false_on_stop(controller, session):
    # Stop cues are never backgrounds; the reducer ignores the flag for them.
    t = session.t
    stop = session.show.library[t["li_stop"]]
    assert stop.background is False
    controller.update_library_item(t["li_stop"], {"background": True})
    assert stop.background is False


def test_update_library_item_ignores_type_change(controller, session):
    # ADR 0006: the meta type is immutable; a client patch carrying "type" is
    # silently dropped -- no type conversion path survives.
    t = session.t
    before = session.show.library[t["li_n1"]].type
    controller.update_library_item(t["li_n1"], {"type": "stop"})
    assert session.show.library[t["li_n1"]].type == before
    controller.update_library_item(t["li_n1"], {"type": "background"})
    assert session.show.library[t["li_n1"]].type == before
    assert session.show.library[t["li_n1"]].background is False


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


# ---------------------------------------------------------------------------
# Chain resolution + scheduling (trigger modes, pre-wait)
# ---------------------------------------------------------------------------
def test_go_on_chain_head_fires_live_and_schedules_follower(controller, session, fake_engine):
    t = session.t
    controller._placement(t["n2"]).trigger_mode = "withPrevious"
    controller._placement(t["n2"]).pre_wait = 0.5
    controller.go()  # head n1 (index 0), follower n2 (withPrevious)
    plays = _placements_named(fake_engine, "play_normal")
    scheds = _placements_named(fake_engine, "schedule_normal")
    assert plays[0][1] == t["n1"]                       # head fired live
    assert scheds[0][1] == t["n2"]                      # follower scheduled
    assert scheds[0][2]["start"] == seconds_to_frames(0.5)


def test_with_previous_start_is_predecessor_fire_plus_prewait(controller, session, fake_engine):
    t = session.t
    controller._placement(t["n2"]).trigger_mode = "withPrevious"
    controller._placement(t["n2"]).pre_wait = 0.25
    controller.go()
    sched = _placements_named(fake_engine, "schedule_normal")[0]
    assert sched[2]["start"] == seconds_to_frames(0.25)


def test_after_previous_start_is_predecessor_end_plus_prewait(controller, session, fake_engine):
    t = session.t
    controller._placement(t["n2"]).trigger_mode = "afterPrevious"
    controller._placement(t["n2"]).pre_wait = 0.1
    controller.go()
    sched = _placements_named(fake_engine, "schedule_normal")[0]
    # head End = trimmed length of n1 (0.5 s), plus the 0.1 s pre-wait.
    assert sched[2]["start"] == seconds_to_frames(0.5) + seconds_to_frames(0.1)


def test_after_previous_behind_looping_predecessor_breaks_chain(controller, session, fake_engine):
    t = session.t
    # bg (index 2) loops; n3 (index 3) chained afterPrevious behind it.
    controller._placement(t["n3"]).trigger_mode = "afterPrevious"
    controller.fire(t["bg"])  # treat bg as head
    assert _placements_named(fake_engine, "play_background")[0][1] == t["bg"]
    # n3's End-based start is undefined -> it is NOT scheduled (falls out).
    assert not any(
        c[1] == t["n3"] for c in _placements_named(fake_engine, "schedule_normal")
    )
    # Cursor parks just past the resolved chain (only bg) -> at n3.
    assert controller.cursors[t["page"]] == 3


def test_chain_does_not_cross_pages(controller, session, fake_engine):
    t = session.t
    # A page-2 whose FIRST placement is withPrevious is still a head.
    controller.add_page("P2")
    p2 = session.show.pages[-1].id
    controller.add_column(p2, "C", 4)
    col = session.show.pages[-1].columns[-1].id
    controller.place_cue(t["li_n1"], p2, col, 0)
    plc = next(p for p in session.show.placements if p.page == p2)
    plc.trigger_mode = "withPrevious"
    controller.set_page(p2)
    controller.go()
    assert fake_engine.last()[0] == "play_normal"
    assert fake_engine.last()[1] == plc.id             # fired live as head


def test_go_parks_cursor_past_entire_chain(controller, session, fake_engine):
    t = session.t
    controller._placement(t["n2"]).trigger_mode = "withPrevious"
    controller._placement(t["bg"]).trigger_mode = "withPrevious"
    controller.go()  # 3-member chain [n1, n2, bg] from index 0
    assert controller.cursors[t["page"]] == 3


def test_fire_mid_chain_treats_target_as_head(controller, session, fake_engine):
    t = session.t
    controller._placement(t["stop"]).trigger_mode = "withPrevious"
    controller._placement(t["stop"]).pre_wait = 0.5
    controller.fire(t["n3"])  # head n3 (index 3), follower stop (index 4)
    assert _placements_named(fake_engine, "play_normal")[0][1] == t["n3"]
    assert _placements_named(fake_engine, "schedule_stop_all_backgrounds")
    assert controller.cursors[t["page"]] == 5          # parked past chain end


def test_zero_prewait_with_previous_fires_live_not_scheduled(controller, session, fake_engine):
    t = session.t
    controller._placement(t["n2"]).trigger_mode = "withPrevious"
    controller._placement(t["n2"]).pre_wait = 0.0
    controller.go()
    plays = _placements_named(fake_engine, "play_normal")
    assert {c[1] for c in plays} == {t["n1"], t["n2"]}   # both live
    assert _placements_named(fake_engine, "schedule_normal") == []


def test_refire_cancels_its_own_pending_followers(controller, session, fake_engine):
    t = session.t
    controller._placement(t["n2"]).trigger_mode = "withPrevious"
    controller._placement(t["n2"]).pre_wait = 0.5
    controller.fire(t["n1"])
    cancels = {c[1] for c in _placements_named(fake_engine, "cancel_scheduled")}
    assert {t["n1"], t["n2"]} <= cancels                # cancels every member first


def test_new_go_does_not_cancel_other_chain(controller, session, fake_engine):
    t = session.t
    # Chain A = [n1, n2]; chain B = [n3, stop].
    controller._placement(t["n2"]).trigger_mode = "withPrevious"
    controller._placement(t["stop"]).trigger_mode = "withPrevious"
    controller._placement(t["stop"]).pre_wait = 0.5
    controller.fire(t["n3"])  # launch chain B only
    cancels = {c[1] for c in _placements_named(fake_engine, "cancel_scheduled")}
    assert t["n1"] not in cancels
    assert t["n2"] not in cancels
    assert {t["n3"], t["stop"]} <= cancels


def test_reset_cancels_all_via_panic(controller, session, fake_engine):
    controller.reset()
    assert ("panic", None, {}) in fake_engine.calls


def test_remove_placement_cancels_pending_via_stop_cue(controller, session, fake_engine):
    t = session.t
    controller.remove_placement(t["n2"])
    assert ("stop_cue", t["n2"], {}) in fake_engine.calls


def test_head_prewait_schedules_head(controller, session, fake_engine):
    t = session.t
    controller._placement(t["n1"]).pre_wait = 0.3  # lone onTrigger with a pre-wait
    controller.go()
    scheds = _placements_named(fake_engine, "schedule_normal")
    assert scheds[0][1] == t["n1"]
    assert scheds[0][2]["start"] == seconds_to_frames(0.3)
    assert _placements_named(fake_engine, "play_normal") == []   # not live


def test_go_lock_applies_to_go_only_followers_bypass(controller, session, fake_engine):
    t = session.t
    controller._placement(t["n2"]).trigger_mode = "withPrevious"
    controller._placement(t["n2"]).pre_wait = 0.5
    controller.go()
    # The follower is armed in the same GO despite the lock now being active.
    assert controller.go_lock_active() is True
    assert _placements_named(fake_engine, "schedule_normal")[0][1] == t["n2"]


def test_update_placement_sets_mode_and_prewait(controller, session):
    t = session.t
    controller.update_placement(t["n2"], {"triggerMode": "withPrevious", "pre_wait": -1.0})
    p = controller._placement(t["n2"])
    assert p.trigger_mode == "withPrevious"
    assert p.pre_wait == 0.0                            # clamped >= 0
    controller.update_placement(t["n2"], {"preWait": 0.5})
    assert controller._placement(t["n2"]).pre_wait == 0.5


def test_update_placement_rejects_after_previous_behind_loop(controller, session):
    t = session.t
    # n3 (index 3) sits right after bg (index 2), which loops.
    controller.update_placement(t["n3"], {"triggerMode": "afterPrevious"})
    assert controller._placement(t["n3"]).trigger_mode == "onTrigger"  # rejected


def test_update_placement_rejects_bad_trigger_mode(controller, session):
    t = session.t
    controller.update_placement(t["n2"], {"triggerMode": "bogus"})
    assert controller._placement(t["n2"]).trigger_mode == "onTrigger"


def test_pause_resume_dispatch_to_engine(controller, fake_engine):
    controller.pause()
    assert fake_engine.last()[0] == "pause_all"
    controller.resume()
    assert fake_engine.last()[0] == "resume_all"


def test_runtime_exposes_paused_and_scheduled(controller, fake_engine):
    fake_engine.status["paused"] = True
    fake_engine.status["scheduled"] = [
        {"cue_id": "p1", "remaining_frames": 24000, "kind": "normal"}
    ]
    rt = controller.build_runtime()
    assert rt["paused"] is True
    assert rt["scheduled"][0]["placementId"] == "p1"
    assert rt["scheduled"][0]["remainingMs"] == 500


def test_dispatch_routes_placement_pause_resume(controller, session, fake_engine):
    t = session.t
    controller.dispatch("updatePlacement", {"placementId": t["n2"], "fields": {"triggerMode": "withPrevious"}})
    assert controller._placement(t["n2"]).trigger_mode == "withPrevious"
    controller.dispatch("pause", {})
    assert fake_engine.last()[0] == "pause_all"
    controller.dispatch("resume", {})
    assert fake_engine.last()[0] == "resume_all"


def test_chained_specific_stop_catches_same_chain_background(controller, session, fake_engine):
    t = session.t
    # Rearrange so bg (background of li_bg) and the stop cue are adjacent, and the
    # stop targets li_bg specifically with a pre-wait so it is scheduled.
    stop_pl = controller._placement(t["stop"])
    stop_pl.column = t["colA"]
    stop_pl.row = 3                                     # directly after bg (colA row2)
    stop_pl.trigger_mode = "withPrevious"
    stop_pl.pre_wait = 0.5
    stop_item = session.show.library[t["li_stop"]]
    stop_item.stop_target = t["li_bg"]
    stop_item.stop_mode = "hard"

    controller.fire(t["bg"])  # head bg (background), follower stop
    scheds = _placements_named(fake_engine, "schedule_stop_background")
    # The stop catches the background started earlier in this very chain, and
    # the pending fire is keyed by the STOP placement's own id (cancellation
    # and armed-cell display key on the stop cell, not its target).
    assert any(c[1] == t["stop"] and c[2]["target"] == t["bg"] for c in scheds)


# ---------------------------------------------------------------------------
# Fade cues (P2 live gain)
# ---------------------------------------------------------------------------
def test_fire_fade_all_backgrounds(controller, session, fake_engine):
    t = session.t
    # Repurpose the stop item as an all-backgrounds fade.
    item = session.show.library[t["li_stop"]]
    item.type = "fade"
    item.fade_target = "allBackgrounds"
    item.fade_to_db = -6.0
    item.fade_time_seconds = 2.0
    controller.fire(t["stop"])
    name, _cid, kw = fake_engine.last()
    assert name == "set_all_backgrounds_gain"
    assert kw["target_db"] == -6.0
    assert kw["ramp_seconds"] == 2.0
    assert kw["stop_when_done"] is False


def test_fire_fade_specific_target_ramps_running_background(controller, session, fake_engine):
    t = session.t
    item = session.show.library[t["li_stop"]]
    item.type = "fade"
    item.fade_target = t["li_bg"]
    item.fade_to_db = -12.0
    item.fade_time_seconds = 1.0
    fake_engine.status["backgrounds"] = [
        {"cue_id": t["bg"], "frame": 0, "total_frames": 100, "loop": True}
    ]
    controller.fire(t["stop"])
    name, cid, kw = fake_engine.last()
    assert name == "set_cue_gain"
    assert cid == t["bg"]
    assert kw["target_db"] == -12.0


def test_fire_fade_specific_target_ramps_running_normal(controller, session, fake_engine):
    t = session.t
    item = session.show.library[t["li_stop"]]
    item.type = "fade"
    item.fade_target = t["li_n1"]
    fake_engine.status["normal"] = {
        "cue_id": t["n1"], "frame": 0, "total_frames": 100, "finished": False
    }
    controller.fire(t["stop"])
    name, cid, _kw = fake_engine.last()
    assert name == "set_cue_gain"
    assert cid == t["n1"]


def test_fade_cue_end_is_fade_time(controller, session, fake_engine):
    t = session.t
    # n1 becomes a fade head; n2 chains afterPrevious behind it.
    li_n1 = session.show.library[t["li_n1"]]
    li_n1.type = "fade"
    li_n1.fade_time_seconds = 2.0
    controller._placement(t["n2"]).trigger_mode = "afterPrevious"
    controller._placement(t["n2"]).pre_wait = 0.1
    controller.go()
    sched = _placements_named(fake_engine, "schedule_normal")[0]
    # Follower starts at the fade's End (= its fade time) plus the pre-wait.
    assert sched[2]["start"] == seconds_to_frames(2.0) + seconds_to_frames(0.1)


def test_scheduled_fade_all_uses_sentinel(controller, session, fake_engine):
    t = session.t
    # n1 head (normal); n2 becomes a withPrevious all-backgrounds fade with pre-wait.
    li_n2 = session.show.library[controller._placement(t["n2"]).library_item_id]
    li_n2.type = "fade"
    li_n2.fade_target = "allBackgrounds"
    li_n2.fade_to_db = -6.0
    li_n2.fade_time_seconds = 2.0
    controller._placement(t["n2"]).trigger_mode = "withPrevious"
    controller._placement(t["n2"]).pre_wait = 0.5
    controller.go()
    sched = _placements_named(fake_engine, "schedule_fade")[0]
    assert sched[1] == t["n2"]                          # keyed by the fade placement id
    assert sched[2]["target"] == ALL_BACKGROUNDS
    assert sched[2]["start"] == seconds_to_frames(0.5)


def test_scheduled_fade_specific_candidates(controller, session, fake_engine):
    t = session.t
    # Chain: bg (head) then a withPrevious fade targeting li_bg, placed just after.
    fade_pl = controller._placement(t["stop"])
    fade_pl.column = t["colA"]
    fade_pl.row = 3                                     # directly after bg (colA row2)
    fade_pl.trigger_mode = "withPrevious"
    fade_pl.pre_wait = 0.5
    fade_item = session.show.library[t["li_stop"]]
    fade_item.type = "fade"
    fade_item.fade_target = t["li_bg"]
    controller.fire(t["bg"])  # head bg, follower fade
    scheds = _placements_named(fake_engine, "schedule_fade")
    # Fade catches the background started earlier in this same chain; the pending
    # fire is keyed by the fade placement's own id.
    assert any(c[1] == t["stop"] and c[2]["target"] == t["bg"] for c in scheds)


def test_update_library_item_gain_live_applies(controller, session, fake_engine):
    t = session.t
    controller.update_library_item(t["li_n1"], {"gainDb": -6.0})
    calls = _placements_named(fake_engine, "set_cue_gain")
    assert any(
        c[1] == t["n1"] and c[2]["target_db"] == -6.0
        and c[2]["ramp_seconds"] == LIVE_GAIN_RAMP_SECONDS
        for c in calls
    )


def test_update_library_item_gain_live_applies_to_audition(controller, session, fake_engine):
    t = session.t
    controller.audition_item(t["li_n1"])
    controller.update_library_item(t["li_n1"], {"gainDb": -3.0})
    calls = _placements_named(fake_engine, "set_cue_gain")
    assert any(c[1] == "__audition__" and c[2]["target_db"] == -3.0 for c in calls)


def test_update_library_item_non_gain_change_no_ramp(controller, session, fake_engine):
    t = session.t
    controller.update_library_item(t["li_n1"], {"fadeIn": 2.0})
    assert _placements_named(fake_engine, "set_cue_gain") == []


def test_normalize_item_live_applies(controller, session, fake_engine):
    t = session.t
    controller.normalize_item(t["li_n1"])
    calls = _placements_named(fake_engine, "set_cue_gain")
    assert any(c[1] == t["n1"] for c in calls)


def test_create_fade_cue_defaults(controller, session):
    t = session.t
    item = controller.create_fade_cue()
    assert item is not None
    assert item.type == "fade"
    assert item.fade_target == "allBackgrounds"
    assert item.fade_time_seconds == 3.0
    assert item.id in session.show.library
    # With a grid cell, it is also placed.
    placed = controller.create_fade_cue(page=t["page"], column=t["colA"], row=3)
    assert any(p.library_item_id == placed.id for p in session.show.placements)


def test_dispatch_routes_create_fade_cue(controller, session):
    item = controller.dispatch("createFadeCue", {})
    assert item is not None
    assert item.type == "fade"
    assert item.id in session.show.library


def test_update_library_item_accepts_fade_fields(controller, session):
    t = session.t
    controller.update_library_item(
        t["li_stop"],
        {
            "fadeTarget": t["li_bg"],
            "fadeToDb": -9.0,
            "fade_time_seconds": 4.0,          # snake-case also accepted
            "fadeStopWhenDone": True,
        },
    )
    item = session.show.library[t["li_stop"]]
    assert item.fade_target == t["li_bg"]
    assert item.fade_to_db == -9.0
    assert item.fade_time_seconds == 4.0
    assert item.fade_stop_when_done is True
