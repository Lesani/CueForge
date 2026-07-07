# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Controller-level tests for compound cues (FakeEngine, no asyncio).

A compound is a LibraryItem(type="compound") carrying a timeline; once rendered
its blob is stored as the ordinary ``audio_hash`` so firing/scheduling/trim math
reuse the normal audio-item code paths with NO compound-specific branches.
"""

from __future__ import annotations

import pytest

from cueforge.audio_format import seconds_to_frames
from cueforge.project.model import make_library_item, make_placement
from cueforge.project.renderer import compound_signature
from cueforge.server import protocol
from cueforge.server.controller import ShowController

from .conftest import HASH_N


def _resolver(session):
    def resolve(sid):
        it = session.show.library.get(sid)
        return it.audio_hash if it is not None else None
    return resolve


def _clip(item_id, **kw):
    c = {"itemId": item_id, "start": 0.0, "clipIn": 0.0, "clipOut": None,
         "gainDb": 0.0, "fadeIn": 0.0, "fadeOut": 0.0, "fadeShape": "linear"}
    c.update(kw)
    return c


# ---------------------------------------------------------------------------
# create / update / render reducers
# ---------------------------------------------------------------------------
def test_create_compound_cue(controller):
    item = controller.create_compound_cue("My Compound")
    assert item is not None
    assert item.type == "compound"
    assert item.timeline == {"tracks": []}
    assert item.render_state == "pending"
    assert controller.session.show.library[item.id] is item


def test_update_timeline_sanitizes_and_marks_pending(controller):
    item = controller.create_compound_cue()
    item.render_state = "ready"
    item.render_error = "old error"
    dirty = {
        "tracks": [
            {"name": "T", "gainDb": "junk", "mute": 1, "clips": [
                {"itemId": "src", "start": "nan", "gainDb": float("inf"),
                 "fadeShape": "bogus"},
                {"foo": "bar"},   # no itemId -> dropped
            ]},
            "not a dict",         # dropped
        ]
    }
    controller.update_timeline(item.id, dirty)
    tl = item.timeline
    assert len(tl["tracks"]) == 1
    tr = tl["tracks"][0]
    assert tr["gainDb"] == 0.0            # "junk" coerced
    assert tr["mute"] is True
    assert len(tr["clips"]) == 1         # clip without itemId dropped
    cl = tr["clips"][0]
    assert cl["start"] == 0.0            # "nan" coerced
    assert cl["gainDb"] == 0.0           # inf coerced
    assert cl["fadeShape"] == "linear"  # bogus coerced
    assert cl["effects"] == []
    assert item.render_state == "pending"
    assert item.render_error == ""


def test_update_timeline_invalid_raises(controller):
    item = controller.create_compound_cue()
    with pytest.raises(ValueError):
        controller.update_timeline(item.id, [1, 2, 3])       # not a dict
    with pytest.raises(ValueError):
        controller.update_timeline(item.id, {"tracks": "nope"})  # tracks not a list


def test_update_timeline_invokes_dirty_hook(controller):
    calls = []
    controller.on_compound_dirty = lambda iid, *, immediate=False: calls.append((iid, immediate))
    item = controller.create_compound_cue()
    controller.update_timeline(item.id, {"tracks": []})
    assert calls == [(item.id, False)]


def test_render_compound_action_invokes_hook_immediate(controller):
    calls = []
    controller.on_compound_dirty = lambda iid, *, immediate=False: calls.append((iid, immediate))
    item = controller.create_compound_cue()
    controller.render_compound(item.id)
    assert calls == [(item.id, True)]


# ---------------------------------------------------------------------------
# Firing: a rendered compound is indistinguishable from an import
# ---------------------------------------------------------------------------
def test_fire_compound_uses_last_render(controller, session):
    show = session.show
    comp = make_library_item(
        "Comp", type="compound", audio_hash=HASH_N, duration=0.5,
        timeline={"tracks": [{"id": "t", "name": "T", "gainDb": 0.0,
                              "mute": False, "clips": [_clip(session.t["li_n1"])]}]},
        render_state="ready",
    )
    show.library[comp.id] = comp
    pl = make_placement(comp.id, session.t["page"], session.t["colA"], 3)
    show.placements.append(pl)

    controller.fire(pl.id)
    assert "play_normal" in controller.engine.names()
    # Fired through the NORMAL branch, not any compound-specific path.
    assert "play_background" not in controller.engine.names()


def test_fire_compound_background_uses_background_bus(controller, session):
    # ADR 0006: a compound with the background role fires on the background bus,
    # looping its rendered blob -- no compound-specific engine work.
    show = session.show
    comp = make_library_item(
        "Amb", type="compound", background=True, loop=True,
        audio_hash=HASH_N, duration=0.5,
        timeline={"tracks": [{"id": "t", "name": "T", "gainDb": 0.0,
                              "mute": False, "clips": [_clip(session.t["li_n1"])]}]},
        render_state="ready",
    )
    show.library[comp.id] = comp
    pl = make_placement(comp.id, session.t["page"], session.t["colA"], 3)
    show.placements.append(pl)

    controller.fire(pl.id)
    assert "play_background" in controller.engine.names()
    assert "play_normal" not in controller.engine.names()
    # Loop is honored for the compound background.
    name, cue_id, kw = controller.engine.last()
    assert name == "play_background"
    assert cue_id == pl.id
    assert kw["loop"] is True


def test_compound_background_loop_has_no_chain_end(controller, session):
    # A looping compound background has an undefined End (loop honored by role).
    comp = make_library_item(
        "Amb", type="compound", background=True, loop=True,
        audio_hash=HASH_N, duration=2.0, render_state="ready",
    )
    assert controller._trimmed_frames(comp) is None
    # Without the background role the same loop flag is ignored -> finite End.
    comp.background = False
    assert controller._trimmed_frames(comp) == seconds_to_frames(2.0)


def test_fire_compound_never_rendered_refuses(controller, session):
    show = session.show
    comp = make_library_item("Comp", type="compound", audio_hash=None,
                             timeline={"tracks": []}, render_state="pending")
    show.library[comp.id] = comp
    pl = make_placement(comp.id, session.t["page"], session.t["colA"], 3)
    show.placements.append(pl)

    with pytest.raises(ValueError):
        controller.fire(pl.id)
    assert "play_normal" not in controller.engine.names()


def test_compound_trimmed_frames_uses_duration(controller):
    comp = make_library_item("Comp", type="compound", audio_hash=HASH_N,
                             duration=2.0, render_state="ready")
    # Falls into the normal (else) branch: End = duration - trim_in.
    assert controller._trimmed_frames(comp) == seconds_to_frames(2.0)


def test_delete_source_leaves_compound_intact(controller, session):
    show = session.show
    src_id = session.t["li_n1"]
    comp = make_library_item(
        "Comp", type="compound",
        timeline={"tracks": [{"id": "t", "name": "T", "gainDb": 0.0,
                              "mute": False, "clips": [_clip(src_id)]}]},
    )
    show.library[comp.id] = comp

    sig_before = compound_signature(comp.timeline, _resolver(session))
    controller.delete_library_item(src_id)

    assert comp.id in show.library                      # compound survives
    assert src_id not in show.library
    sig_after = compound_signature(comp.timeline, _resolver(session))
    assert sig_before != sig_after                      # source hash -> None


def test_delete_source_marks_compound_dirty(controller, session):
    """Amendment 3: deleting a referenced source schedules a re-render."""
    calls = []
    controller.on_compound_dirty = lambda iid, *, immediate=False: calls.append(iid)
    show = session.show
    src_id = session.t["li_n1"]
    comp = make_library_item(
        "Comp", type="compound",
        timeline={"tracks": [{"id": "t", "name": "T", "gainDb": 0.0,
                              "mute": False, "clips": [_clip(src_id)]}]},
    )
    show.library[comp.id] = comp
    controller.delete_library_item(src_id)
    assert calls == [comp.id]


def test_dispatch_routes_compound_actions(controller):
    item = controller.dispatch(protocol.CREATE_COMPOUND, {"name": "X"})
    assert item.type == "compound"
    # updateTimeline + renderCompound reach the reducers (no exception, state set).
    controller.dispatch(protocol.UPDATE_TIMELINE, {"itemId": item.id, "timeline": {"tracks": []}})
    assert item.render_state == "pending"
    controller.dispatch(protocol.RENDER_COMPOUND, {"itemId": item.id})
    assert item.render_state == "pending"


# ---------------------------------------------------------------------------
# Amendment 4: an unrendered compound in a chain resolves as zero-length
# ---------------------------------------------------------------------------
def test_chain_after_unrendered_compound_zero_length(controller, session):
    show = session.show
    comp = make_library_item("Comp", type="compound", audio_hash=None,
                             duration=0.0, timeline={"tracks": []},
                             render_state="pending")
    show.library[comp.id] = comp
    p_comp = make_placement(comp.id, session.t["page"], session.t["colA"], 3)
    p_succ = make_placement(session.t["li_n1"], session.t["page"], session.t["colA"], 4,
                            trigger_mode="afterPrevious", pre_wait=0.0)
    show.placements.extend([p_comp, p_succ])

    # Chain resolution is pure model math (no PCM) and must not crash: an
    # unrendered compound contributes a zero-length End, so the afterPrevious
    # successor starts at the compound's own start offset (0 here).
    resolved, count = controller._resolve_chain([p_comp, p_succ])
    assert count == 2
    assert resolved[0][1] == 0            # compound head start
    assert resolved[1][1] == 0            # successor: prev_end (0) + pre_wait (0)

    # Firing the unrendered compound live still refuses the GO (no audio), which
    # is surfaced to the client as an error frame by the app layer.
    with pytest.raises(ValueError):
        controller.fire(p_comp.id)


def test_duplicate_compound_deep_copies_timeline(controller):
    comp = controller.create_compound_cue("Orig")
    controller.update_timeline(comp.id, {"tracks": [{"name": "T1", "clips": []}]})
    clone = controller.duplicate_library_item(comp.id)
    assert clone.timeline is not comp.timeline
    assert clone.timeline["tracks"] is not comp.timeline["tracks"]
    # Editing the clone must never leak into the original.
    controller.update_timeline(clone.id, {"tracks": []})
    assert comp.timeline["tracks"] != []
