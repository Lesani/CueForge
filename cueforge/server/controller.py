# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""ShowController -- the authoritative runtime + reducer for CueForge.

This is the CORE of the server subsystem. It bridges the persisted show model
(:mod:`cueforge.project`) and the real-time :class:`~cueforge.engine.AudioEngine`,
and it is fully unit-testable WITHOUT FastAPI or a real audio device: the engine
is injected (tests pass a FakeEngine) and time is injectable for the GO lock.

Cue identity for the engine
---------------------------
The PLACEMENT id is used as the engine ``cue_id``. Re-firing the same placement
therefore restarts that voice, and two placements of one library item are
distinct engine voices. The 15 Hz status broadcast maps engine ``cue_id`` values
straight back to placement ids.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Optional

from cueforge.project import (
    ProjectSession,
    add_clone,  # noqa: F401  (re-exported convenience for the app layer)
    cue_engine_params,
    load_cue_pcm,
    make_column,
    make_page,
    make_placement,
    normalize,
    page_cue_sequence,
    placement_at,
)
from cueforge.project.model import CuePlacement, LibraryItem
from cueforge.server import protocol

# Global anti-double-tap GO lock.
GO_LOCK_SECONDS = 0.5
# Default row count for a freshly added column.
DEFAULT_COLUMN_ROWS = 8

# Accept both model attribute names (snake_case) and wire names (camelCase)
# when updating a library item.
_FIELD_ALIASES = {
    "trimIn": "trim_in",
    "trimOut": "trim_out",
    "gainDb": "gain_db",
    "fadeIn": "fade_in",
    "fadeOut": "fade_out",
    "fadeShape": "fade_shape",
    "stopTarget": "stop_target",
    "stopMode": "stop_mode",
    "stopFadeSeconds": "stop_fade_seconds",
}
_ALLOWED_FIELDS = {
    "name",
    "type",
    "trim_in",
    "trim_out",
    "gain_db",
    "fade_in",
    "fade_out",
    "fade_shape",
    "loop",
    "group",
    "stop_target",
    "stop_mode",
    "stop_fade_seconds",
}


class ShowController:
    """Authoritative runtime state + reducer bridging model and engine."""

    def __init__(
        self,
        engine,
        session: Optional[ProjectSession] = None,
        *,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self._now = time_func

        self.session: Optional[ProjectSession] = None
        self.current_page: Optional[str] = None
        self.edit_mode: bool = False
        self.cursors: dict[str, int] = {}
        self.go_lock_until: float = 0.0
        self.loading: Optional[dict] = None
        # Which library item is on the engine's audition channel (for the
        # per-client playhead marker). Set on audition, cleared on stop or when
        # the engine reports the audition voice has ended.
        self._audition_item_id: Optional[str] = None

        if session is not None:
            self.set_session(session)

    # =====================================================================
    # Session wiring
    # =====================================================================
    def set_session(self, session: Optional[ProjectSession]) -> None:
        """Attach (or clear) the open project and reset transient runtime state."""
        self.session = session
        self.cursors = {}
        self.current_page = None
        if session is not None:
            for page in session.show.pages:
                self.cursors[page.id] = 0
            if session.show.pages:
                self.current_page = session.show.pages[0].id

    @property
    def show(self):
        return self.session.show if self.session is not None else None

    # =====================================================================
    # GO lock helpers
    # =====================================================================
    def go_lock_active(self) -> bool:
        return self._now() < self.go_lock_until

    def go_lock_remaining_ms(self) -> int:
        remaining = self.go_lock_until - self._now()
        return max(0, int(round(remaining * 1000)))

    # =====================================================================
    # Internal model lookups
    # =====================================================================
    def _ready(self) -> bool:
        return self.session is not None and self.current_page is not None

    def _sequence(self, page_id: Optional[str] = None) -> list[CuePlacement]:
        if self.session is None:
            return []
        pid = page_id if page_id is not None else self.current_page
        if pid is None:
            return []
        return page_cue_sequence(self.session.show, pid)

    def _cursor(self, page_id: Optional[str] = None) -> int:
        pid = page_id if page_id is not None else self.current_page
        seq_len = len(self._sequence(pid))
        raw = self.cursors.get(pid, 0)
        return max(0, min(raw, seq_len))

    def _placement(self, placement_id: str) -> Optional[CuePlacement]:
        if self.session is None:
            return None
        for p in self.session.show.placements:
            if p.id == placement_id:
                return p
        return None

    def _library_item(self, library_item_id: str) -> Optional[LibraryItem]:
        if self.session is None:
            return None
        return self.session.show.library.get(library_item_id)

    def _column_page(self, column_id: str):
        if self.session is None:
            return None, None
        for page in self.session.show.pages:
            for col in page.columns:
                if col.id == column_id:
                    return page, col
        return None, None

    def _page(self, page_id: str):
        if self.session is None:
            return None
        for page in self.session.show.pages:
            if page.id == page_id:
                return page
        return None

    @staticmethod
    def _index_of(sequence: list[CuePlacement], placement_id: str) -> Optional[int]:
        for i, p in enumerate(sequence):
            if p.id == placement_id:
                return i
        return None

    def _autosave(self) -> None:
        if self.session is not None:
            self.session.autosave()

    # =====================================================================
    # Firing engine
    # =====================================================================
    def _fire_placement(self, placement: CuePlacement) -> None:
        item = self._library_item(placement.library_item_id)
        if item is None:
            return

        if item.type == "stop":
            self._fire_stop(item)
            return

        pcm = load_cue_pcm(self.session, item)
        p = cue_engine_params(item)
        if item.type == "background":
            self.engine.play_background(
                placement.id,
                pcm,
                gain_db=p["gain_db"],
                fade_in=p["fade_in"],
                loop=item.loop,
                fade_shape=p["fade_shape"],
            )
        else:  # "normal"
            self.engine.play_normal(
                placement.id,
                pcm,
                gain_db=p["gain_db"],
                fade_in=p["fade_in"],
                fade_out=p["fade_out"],
                fade_shape=p["fade_shape"],
            )

    def _fire_stop(self, item: LibraryItem) -> None:
        if item.stop_target == "allBackgrounds":
            self.engine.stop_all_backgrounds(
                mode=item.stop_mode, fade_seconds=item.stop_fade_seconds
            )
            return
        # Stop the running background placement(s) whose library item matches the
        # stop target. The engine is keyed by placement id, so we resolve the
        # currently-running background cue_ids back to placements.
        status = self.engine.get_status()
        for bg in status.get("backgrounds", []):
            placement = self._placement(bg["cue_id"])
            if placement is not None and placement.library_item_id == item.stop_target:
                self.engine.stop_background(
                    placement.id,
                    mode=item.stop_mode,
                    fade_seconds=item.stop_fade_seconds,
                )

    # =====================================================================
    # Playback reducer actions
    # =====================================================================
    def go(self) -> None:
        if not self._ready():
            return
        if self.go_lock_active():
            return
        seq = self._sequence()
        idx = self._cursor()
        if idx >= len(seq):
            return  # parked at end: GO does nothing further
        self._fire_placement(seq[idx])
        self.cursors[self.current_page] = min(idx + 1, len(seq))
        self.go_lock_until = self._now() + GO_LOCK_SECONDS

    def fire(self, placement_id: str) -> None:
        if self.session is None:
            return
        placement = self._placement(placement_id)
        if placement is None:
            return
        self._fire_placement(placement)
        seq = self._sequence(placement.page)
        idx = self._index_of(seq, placement_id)
        if idx is not None:
            self.cursors[placement.page] = min(idx + 1, len(seq))

    def standby(self, placement_id: str) -> None:
        if self.session is None:
            return
        placement = self._placement(placement_id)
        if placement is None:
            return
        seq = self._sequence(placement.page)
        idx = self._index_of(seq, placement_id)
        if idx is not None:
            self.cursors[placement.page] = idx

    def cursor_move(self, direction: str) -> None:
        if not self._ready():
            return
        seq = self._sequence()
        if not seq:
            self.cursors[self.current_page] = 0
            return
        idx = self._cursor()

        if direction == "up":
            self.cursors[self.current_page] = max(0, idx - 1)
            return
        if direction == "down":
            self.cursors[self.current_page] = min(len(seq), idx + 1)
            return
        if direction in ("left", "right"):
            self._cursor_jump_column(seq, idx, direction)

    def _cursor_jump_column(
        self, seq: list[CuePlacement], idx: int, direction: str
    ) -> None:
        # Ordered unique columns as they appear in the column-major sequence,
        # with the sequence index of each column's first cue.
        columns: list[str] = []
        first_index: dict[str, int] = {}
        for i, p in enumerate(seq):
            if p.column not in first_index:
                first_index[p.column] = i
                columns.append(p.column)

        # Column of the cue currently under the cursor (clamp when parked).
        cur_placement = seq[min(idx, len(seq) - 1)]
        cur_col = cur_placement.column
        pos = columns.index(cur_col)
        if direction == "left":
            pos = max(0, pos - 1)
        else:  # right
            pos = min(len(columns) - 1, pos + 1)
        self.cursors[self.current_page] = first_index[columns[pos]]

    def set_page(self, page_id: str) -> None:
        if self.session is None:
            return
        if self._page(page_id) is None:
            return
        self.current_page = page_id
        self.cursors.setdefault(page_id, 0)

    def set_edit_mode(self, on: bool) -> None:
        self.edit_mode = bool(on)

    def panic(self) -> None:
        self.engine.panic()

    def reset(self) -> None:
        for page_id in list(self.cursors.keys()):
            self.cursors[page_id] = 0
        if self.session is not None:
            for page in self.session.show.pages:
                self.cursors[page.id] = 0
        self.engine.panic()

    # =====================================================================
    # Edit-mode reducer actions (all autosave)
    # =====================================================================
    def place_cue(self, library_item_id: str, page: str, column: str, row: int) -> None:
        if self.session is None:
            return
        placement = make_placement(library_item_id, page, column, int(row))
        self.session.show.placements.append(placement)
        self._autosave()

    def move_cue(self, placement_id: str, to_column: str, to_row: int) -> None:
        if self.session is None:
            return
        placement = self._placement(placement_id)
        if placement is None:
            return
        to_row = int(to_row)
        target = placement_at(self.session.show, placement.page, to_column, to_row)
        if target is not None and target.id != placement.id:
            # Swap the two placements' positions.
            target.column, target.row = placement.column, placement.row
            placement.column, placement.row = to_column, to_row
        else:
            placement.column = to_column
            placement.row = to_row
        self._autosave()

    def remove_placement(self, placement_id: str) -> None:
        if self.session is None:
            return
        # Stop any audio this placement is currently playing so it dies with the
        # cell (the engine is keyed by placement id).
        self.engine.stop_cue(placement_id)
        show = self.session.show
        show.placements = [p for p in show.placements if p.id != placement_id]
        self._autosave()

    def delete_library_item(self, library_item_id: str) -> None:
        if self.session is None:
            return
        show = self.session.show
        # Stop any playing voices for placements of this item before removing them.
        for p in show.placements:
            if p.library_item_id == library_item_id:
                self.engine.stop_cue(p.id)
        show.library.pop(library_item_id, None)
        show.placements = [
            p for p in show.placements if p.library_item_id != library_item_id
        ]
        self._autosave()

    def add_column(self, page: str, name: str, rows: int = DEFAULT_COLUMN_ROWS) -> None:
        page_obj = self._page(page)
        if page_obj is None:
            return
        page_obj.columns.append(make_column(name, int(rows)))
        self._autosave()

    def rename_column(self, column_id: str, name: str) -> None:
        _page, col = self._column_page(column_id)
        if col is None:
            return
        col.name = name
        self._autosave()

    def remove_column(self, column_id: str) -> None:
        page, col = self._column_page(column_id)
        if col is None:
            return
        for p in self.session.show.placements:
            if p.column == column_id:
                self.engine.stop_cue(p.id)
        page.columns = [c for c in page.columns if c.id != column_id]
        self.session.show.placements = [
            p for p in self.session.show.placements if p.column != column_id
        ]
        self._autosave()

    def set_rows(self, column_id: str, rows: int) -> None:
        _page, col = self._column_page(column_id)
        if col is None:
            return
        col.rows = int(rows)
        self._autosave()

    def add_page(self, name: str) -> None:
        if self.session is None:
            return
        page = make_page(name)
        self.session.show.pages.append(page)
        self.cursors[page.id] = 0
        if self.current_page is None:
            self.current_page = page.id
        self._autosave()

    def rename_page(self, page_id: str, name: str) -> None:
        page = self._page(page_id)
        if page is None:
            return
        page.name = name
        self._autosave()

    def remove_page(self, page_id: str) -> None:
        if self.session is None:
            return
        show = self.session.show
        for p in show.placements:
            if p.page == page_id:
                self.engine.stop_cue(p.id)
        show.pages = [p for p in show.pages if p.id != page_id]
        show.placements = [p for p in show.placements if p.page != page_id]
        self.cursors.pop(page_id, None)
        if self.current_page == page_id:
            self.current_page = show.pages[0].id if show.pages else None
        self._autosave()

    # =====================================================================
    # Library reducer actions (all autosave)
    # =====================================================================
    def update_library_item(self, library_item_id: str, fields: dict) -> None:
        item = self._library_item(library_item_id)
        if item is None:
            return
        for key, value in (fields or {}).items():
            attr = _FIELD_ALIASES.get(key, key)
            if attr in _ALLOWED_FIELDS:
                setattr(item, attr, value)
        self._autosave()

    def duplicate_library_item(self, library_item_id: str) -> Optional[LibraryItem]:
        from cueforge.project import new_id

        item = self._library_item(library_item_id)
        if item is None:
            return None
        clone = replace(item, id=new_id(), name=f"{item.name} (copy)")
        self.session.show.library[clone.id] = clone
        self._autosave()
        return clone

    def create_stop_cue(
        self,
        name: Optional[str] = None,
        page: Optional[str] = None,
        column: Optional[str] = None,
        row: Optional[int] = None,
    ) -> Optional[LibraryItem]:
        """Create an audio-less "stop all backgrounds" control cue from scratch.

        Optionally also place it at a grid cell in the same action (the cue
        picker's "new stop cue here" shortcut -- the client has no other way
        to learn the fresh item's id before the snapshot broadcast).
        """
        if self.session is None:
            return None
        from cueforge.project import make_library_item

        item = make_library_item(
            name or "Stop all backgrounds",
            type="stop",
            stop_target="allBackgrounds",
            stop_mode="hard",
            stop_fade_seconds=0.0,
        )
        self.session.show.library[item.id] = item
        if page is not None and column is not None and row is not None:
            placement = make_placement(item.id, page, column, int(row))
            self.session.show.placements.append(placement)
        self._autosave()
        return item

    def normalize_item(self, library_item_id: str) -> None:
        item = self._library_item(library_item_id)
        if item is None or item.audio_hash is None:
            return
        normalize(self.session, item)  # autosaves internally

    # =====================================================================
    # Audition (server-out)
    # =====================================================================
    def audition_item(self, library_item_id: str) -> None:
        item = self._library_item(library_item_id)
        if item is None or item.audio_hash is None:
            return
        pcm = load_cue_pcm(self.session, item)
        p = cue_engine_params(item)
        self.engine.audition(
            pcm,
            gain_db=p["gain_db"],
            fade_in=p["fade_in"],
            fade_out=p["fade_out"],
            fade_shape=p["fade_shape"],
            loop=item.loop,
        )
        self._audition_item_id = library_item_id

    def stop_audition(self) -> None:
        self.engine.stop_audition()
        self._audition_item_id = None

    # =====================================================================
    # Snapshot building
    # =====================================================================
    def build_runtime(self, clients: int = 0) -> dict:
        """Build the ``runtime`` block of the state snapshot (reads engine status)."""
        seq = self._sequence()
        seq_ids = [p.id for p in seq]
        status = self.engine.get_status()
        mapped = protocol.map_engine_status(status)

        # Attach the auditioning library item id to the position block so each
        # client draws the playhead only for its own selected item. When the
        # engine reports no audition voice, forget the tracked id.
        audition = mapped.get("audition")
        if audition is None:
            self._audition_item_id = None
        else:
            audition = {**audition, "libraryItemId": self._audition_item_id}

        return {
            "currentPage": self.current_page,
            "editMode": self.edit_mode,
            "sequence": seq_ids,
            "cursorIndex": self._cursor(),
            "playing": mapped["playing"],
            "backgrounds": mapped["backgrounds"],
            "auditionActive": mapped.get("auditionActive", False),
            "audition": audition,
            "goLockRemainingMs": self.go_lock_remaining_ms(),
            "deviceOk": bool(status.get("device_ok", False)),
            "loading": self.loading,
            "clients": clients,
        }

    def build_snapshot(self, clients: int = 0) -> dict:
        """Build the full ``{"type":"state", ...}`` snapshot message."""
        return protocol.state_message(self.show, self.build_runtime(clients))

    # =====================================================================
    # WebSocket action dispatch
    # =====================================================================
    def dispatch(self, action: str, params: Optional[dict] = None):
        """Route a WS action + params to the matching reducer method."""
        params = params or {}
        if action == protocol.GO:
            return self.go()
        if action == protocol.FIRE:
            return self.fire(params["placementId"])
        if action == protocol.STANDBY:
            return self.standby(params["placementId"])
        if action == protocol.CURSOR_MOVE:
            return self.cursor_move(params["direction"])
        if action == protocol.SET_PAGE:
            return self.set_page(params["pageId"])
        if action == protocol.SET_EDIT_MODE:
            return self.set_edit_mode(params["on"])
        if action == protocol.PANIC:
            return self.panic()
        if action == protocol.RESET:
            return self.reset()
        if action == protocol.PLACE_CUE:
            return self.place_cue(
                params["libraryItemId"], params["page"], params["column"], params["row"]
            )
        if action == protocol.MOVE_CUE:
            return self.move_cue(
                params["placementId"], params["toColumn"], params["toRow"]
            )
        if action == protocol.REMOVE_PLACEMENT:
            return self.remove_placement(params["placementId"])
        if action == protocol.DELETE_LIBRARY_ITEM:
            return self.delete_library_item(params["libraryItemId"])
        if action == protocol.ADD_COLUMN:
            return self.add_column(
                params["page"], params["name"], params.get("rows", DEFAULT_COLUMN_ROWS)
            )
        if action == protocol.RENAME_COLUMN:
            return self.rename_column(params["columnId"], params["name"])
        if action == protocol.REMOVE_COLUMN:
            return self.remove_column(params["columnId"])
        if action == protocol.SET_ROWS:
            return self.set_rows(params["columnId"], params["rows"])
        if action == protocol.ADD_PAGE:
            return self.add_page(params["name"])
        if action == protocol.RENAME_PAGE:
            return self.rename_page(params["pageId"], params["name"])
        if action == protocol.REMOVE_PAGE:
            return self.remove_page(params["pageId"])
        if action == protocol.UPDATE_LIBRARY_ITEM:
            return self.update_library_item(
                params["libraryItemId"], params.get("fields", {})
            )
        if action == protocol.DUPLICATE_LIBRARY_ITEM:
            return self.duplicate_library_item(params["libraryItemId"])
        if action == protocol.CREATE_STOP_CUE:
            return self.create_stop_cue(
                params.get("name"),
                params.get("page"),
                params.get("column"),
                params.get("row"),
            )
        if action == protocol.NORMALIZE_ITEM:
            return self.normalize_item(params["libraryItemId"])
        if action == protocol.AUDITION_ITEM:
            return self.audition_item(params["libraryItemId"])
        if action == protocol.STOP_AUDITION:
            return self.stop_audition()
        raise ValueError(f"unknown action: {action!r}")
