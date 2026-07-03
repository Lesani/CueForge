# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""createStopCue action + the group label field on library items."""

from __future__ import annotations

from cueforge.project.model import LibraryItem
from cueforge.project.storage import ProjectSession


# ---------------------------------------------------------------- createStopCue

def test_create_stop_cue_default_name(controller):
    before = len(controller.session.show.library)
    controller.dispatch("createStopCue", {})
    library = controller.session.show.library
    assert len(library) == before + 1
    new_items = [i for i in library.values() if i.type == "stop"
                 and i.name == "Stop all backgrounds"]
    assert len(new_items) == 1
    item = new_items[0]
    assert item.audio_hash is None
    assert item.stop_target == "allBackgrounds"
    assert item.stop_mode == "hard"
    assert item.stop_fade_seconds == 0.0


def test_create_stop_cue_custom_name(controller):
    controller.dispatch("createStopCue", {"name": "Kill music"})
    names = [i.name for i in controller.session.show.library.values()]
    assert "Kill music" in names


def test_create_stop_cue_autosaves(controller):
    import json

    controller.dispatch("createStopCue", {"name": "Kill music"})
    # Reload show.json from the working folder: the new cue must have persisted.
    with open(controller.session._show_json_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    names = [v["name"] for v in data["library"].values()]
    assert "Kill music" in names


# ---------------------------------------------------------------- group field

def test_group_updates_via_update_library_item(controller):
    item_id = controller.session.t["li_n1"]
    controller.dispatch(
        "updateLibraryItem", {"libraryItemId": item_id, "fields": {"group": "Act 1"}}
    )
    assert controller.session.show.library[item_id].group == "Act 1"


def test_group_defaults_empty_and_round_trips_show_json(tmp_path):
    session = ProjectSession.create_new(str(tmp_path / "work"), "Show")
    item = LibraryItem(id="x", name="Cue", group="Scene 2")
    session.show.library[item.id] = item
    assert item.group == "Scene 2"

    # to_dict includes group.
    assert item.to_dict()["group"] == "Scene 2"

    session.save_as(str(tmp_path / "out.cueforge"))
    reopened = ProjectSession.open(str(tmp_path / "out.cueforge"), str(tmp_path / "w2"))
    assert reopened.show.library["x"].group == "Scene 2"


def test_group_missing_in_old_show_defaults_empty():
    # from_dict tolerates old files with no "group"/"folder" key.
    item = LibraryItem.from_dict({"id": "x", "name": "Old"})
    assert item.group == ""


def test_legacy_folder_key_loads_as_group():
    # Shows saved before the rename carry "folder"; it must load into "group".
    item = LibraryItem.from_dict({"id": "x", "name": "Old", "folder": "Act 3"})
    assert item.group == "Act 3"
    # The new "group" key wins when both are present.
    both = LibraryItem.from_dict(
        {"id": "y", "name": "New", "folder": "Old", "group": "New"}
    )
    assert both.group == "New"
