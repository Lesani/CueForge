# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""WebSocket/REST action names and snapshot (dict) shaping.

This module is intentionally free of any FastAPI / audio / IO dependency: it
holds the protocol *constants* and the pure functions that shape outgoing
messages. The authoritative runtime lives in :mod:`cueforge.server.controller`.
"""

from __future__ import annotations

from cueforge.audio_format import frames_to_seconds

# ---------------------------------------------------------------------------
# WebSocket action names (client -> server). See PROTOCOL.md.
# ---------------------------------------------------------------------------
GO = "go"
FIRE = "fire"
STANDBY = "standby"
CURSOR_MOVE = "cursorMove"
SET_PAGE = "setPage"
SET_EDIT_MODE = "setEditMode"
PANIC = "panic"
RESET = "reset"

PLACE_CUE = "placeCue"
MOVE_CUE = "moveCue"
REMOVE_PLACEMENT = "removePlacement"
DELETE_LIBRARY_ITEM = "deleteLibraryItem"
ADD_COLUMN = "addColumn"
RENAME_COLUMN = "renameColumn"
REMOVE_COLUMN = "removeColumn"
SET_ROWS = "setRows"
ADD_PAGE = "addPage"
RENAME_PAGE = "renamePage"
REMOVE_PAGE = "removePage"

UPDATE_LIBRARY_ITEM = "updateLibraryItem"
DUPLICATE_LIBRARY_ITEM = "duplicateLibraryItem"
CREATE_STOP_CUE = "createStopCue"
CREATE_FADE_CUE = "createFadeCue"
NORMALIZE_ITEM = "normalizeItem"
AUDITION_ITEM = "auditionItem"
STOP_AUDITION = "stopAudition"

UPDATE_PLACEMENT = "updatePlacement"
PAUSE = "pause"
RESUME = "resume"

# Compound cues (see PROTOCOL.md and ADR 0004).
CREATE_COMPOUND = "createCompound"
UPDATE_TIMELINE = "updateTimeline"
RENDER_COMPOUND = "renderCompound"

SET_OUTPUTS = "setOutputs"
TEST_OUTPUT = "testOutput"

# Every action the server understands over the WebSocket.
ACTIONS = frozenset(
    {
        GO,
        FIRE,
        STANDBY,
        CURSOR_MOVE,
        SET_PAGE,
        SET_EDIT_MODE,
        PANIC,
        RESET,
        PLACE_CUE,
        MOVE_CUE,
        REMOVE_PLACEMENT,
        DELETE_LIBRARY_ITEM,
        ADD_COLUMN,
        RENAME_COLUMN,
        REMOVE_COLUMN,
        SET_ROWS,
        ADD_PAGE,
        RENAME_PAGE,
        REMOVE_PAGE,
        UPDATE_LIBRARY_ITEM,
        DUPLICATE_LIBRARY_ITEM,
        CREATE_STOP_CUE,
        CREATE_FADE_CUE,
        NORMALIZE_ITEM,
        AUDITION_ITEM,
        STOP_AUDITION,
        UPDATE_PLACEMENT,
        PAUSE,
        RESUME,
        SET_OUTPUTS,
        TEST_OUTPUT,
        CREATE_COMPOUND,
        UPDATE_TIMELINE,
        RENDER_COMPOUND,
    }
)


# ---------------------------------------------------------------------------
# Message shaping
# ---------------------------------------------------------------------------
def state_message(show, runtime: dict) -> dict:
    """Wrap a Show + runtime dict into the ``{"type":"state", ...}`` snapshot."""
    return {
        "type": "state",
        "show": show.to_dict() if show is not None else None,
        "runtime": runtime,
    }


def error_message(message: str) -> dict:
    """Shape an error frame sent to a single client."""
    return {"type": "error", "message": str(message)}


def map_engine_status(status: dict, placement_ids: set[str] | None = None) -> dict:
    """Map an ``AudioEngine.get_status()`` dict to runtime ``playing``/``backgrounds``.

    The engine is keyed by the PLACEMENT id (used as the engine ``cue_id``), so
    each running voice's ``cue_id`` maps directly back to a placement id.
    """
    normal = status.get("normal")
    playing = None
    if normal and not normal.get("finished"):
        cid = normal["cue_id"]
        playing = {
            "placementId": cid,
            "cueId": cid,
            "frame": normal.get("frame", 0),
            "totalFrames": normal.get("total_frames", 0),
        }

    backgrounds = []
    for b in status.get("backgrounds", []):
        cid = b["cue_id"]
        backgrounds.append(
            {
                "placementId": cid,
                "cueId": cid,
                "frame": b.get("frame", 0),
                "totalFrames": b.get("total_frames", 0),
                "loop": bool(b.get("loop", False)),
            }
        )
    audition = status.get("audition")
    audition_pos = None
    if audition and not audition.get("finished"):
        audition_pos = {
            "frame": audition.get("frame", 0),
            "totalFrames": audition.get("total_frames", 0),
        }

    # Pause + pending chain fires. Read tolerantly: a FakeEngine or an older
    # caller may omit both keys, in which case they default to not-paused/empty.
    paused = bool(status.get("paused", False))
    scheduled = [
        {
            "placementId": s["cue_id"],
            "cueId": s["cue_id"],
            "kind": s.get("kind", "normal"),
            "remainingMs": int(round(frames_to_seconds(s.get("remaining_frames", 0)) * 1000)),
        }
        for s in status.get("scheduled", [])
    ]
    return {
        "playing": playing,
        "backgrounds": backgrounds,
        "auditionActive": bool(status.get("audition_active", False)),
        "audition": audition_pos,
        "paused": paused,
        "scheduled": scheduled,
        "outputs": status.get("outputs", []),
    }
