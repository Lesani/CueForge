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
from copy import deepcopy
from dataclasses import replace
from typing import Callable, Optional

from cueforge.audio_format import seconds_to_frames
from cueforge.engine.audio_engine import ALL_BACKGROUNDS
from cueforge.project import (
    ProjectSession,
    add_clone,  # noqa: F401  (re-exported convenience for the app layer)
    cue_engine_params,
    load_cue_pcm,
    make_column,
    make_page,
    make_placement,
    new_id,
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
# Smoothing ramp for a live stored-gain edit applied to a running voice.
LIVE_GAIN_RAMP_SECONDS = 0.050

# Accept both model attribute names (snake_case) and wire names (camelCase)
# when updating a library item.
_FIELD_ALIASES = {
    "trimIn": "trim_in",
    "trimOut": "trim_out",
    "gainDb": "gain_db",
    "fadeIn": "fade_in",
    "fadeOut": "fade_out",
    "fadeShape": "fade_shape",
    "outputId": "output_id",
    "stopTarget": "stop_target",
    "stopMode": "stop_mode",
    "stopFadeSeconds": "stop_fade_seconds",
    "fadeTarget": "fade_target",
    "fadeToDb": "fade_to_db",
    "fadeTimeSeconds": "fade_time_seconds",
    "fadeStopWhenDone": "fade_stop_when_done",
}
_ALLOWED_FIELDS = {
    "name",
    # NOTE: "type" is deliberately NOT here -- the meta type is immutable after
    # creation (ADR 0006). The old type-conversion path is gone; a client that
    # still sends "type" is silently ignored.
    "background",
    "trim_in",
    "trim_out",
    "gain_db",
    "fade_in",
    "fade_out",
    "fade_shape",
    "loop",
    "output_id",
    "group",
    "stop_target",
    "stop_mode",
    "stop_fade_seconds",
    "fade_target",
    "fade_to_db",
    "fade_time_seconds",
    "fade_stop_when_done",
}

# Placement sequencing patch (updatePlacement): camelCase wire names accepted
# alongside the snake_case model attributes.
_PLACEMENT_FIELD_ALIASES = {
    "triggerMode": "trigger_mode",
    "preWait": "pre_wait",
    "outputId": "output_id",
}
_ALLOWED_PLACEMENT_FIELDS = {"trigger_mode", "pre_wait", "output_id"}
_TRIGGER_MODES = {"onTrigger", "withPrevious", "afterPrevious"}

# Compound-cue timeline sanitization.
_FADE_SHAPES = {"linear", "equalPower"}


def _sanitize_timeline(timeline: dict) -> dict:
    """Coerce a client timeline into a clean, safe structure. Raises ValueError
    only on a fundamentally malformed shape (not a dict / tracks not a list)."""
    if not isinstance(timeline, dict):
        raise ValueError("timeline must be an object")
    tracks_in = timeline.get("tracks", [])
    if not isinstance(tracks_in, list):
        raise ValueError("timeline.tracks must be a list")

    def num(v, lo=None):
        try:
            f = float(v)
        except (TypeError, ValueError):
            f = 0.0
        if f != f or f in (float("inf"), float("-inf")):
            f = 0.0
        return max(lo, f) if lo is not None else f

    tracks_out = []
    for tr in tracks_in:
        if not isinstance(tr, dict):
            continue
        clips_out = []
        for cl in (tr.get("clips") or []):
            if not isinstance(cl, dict) or not cl.get("itemId"):
                continue
            co = cl.get("clipOut")
            clips_out.append({
                "id": str(cl.get("id") or new_id()),
                "itemId": str(cl.get("itemId")),
                "start": num(cl.get("start"), 0.0),
                "clipIn": num(cl.get("clipIn"), 0.0),
                "clipOut": None if co is None else num(co, 0.0),
                "gainDb": num(cl.get("gainDb")),
                "fadeIn": num(cl.get("fadeIn"), 0.0),
                "fadeOut": num(cl.get("fadeOut"), 0.0),
                "fadeShape": cl.get("fadeShape") if cl.get("fadeShape") in _FADE_SHAPES else "linear",
                "effects": cl.get("effects") if isinstance(cl.get("effects"), list) else [],
            })
        tracks_out.append({
            "id": str(tr.get("id") or new_id()),
            "name": str(tr.get("name") or "Track"),
            "gainDb": num(tr.get("gainDb")),
            "mute": bool(tr.get("mute", False)),
            "clips": clips_out,
        })
    return {"tracks": tracks_out}


def _timeline_references(timeline: Optional[dict], item_id: str) -> bool:
    """True if any clip in ``timeline`` references source ``item_id``."""
    for tr in (timeline or {}).get("tracks", []) or []:
        for cl in tr.get("clips", []) or []:
            if cl.get("itemId") == item_id:
                return True
    return False


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

        # Set by the app layer to schedule a debounced background render. Signature:
        # on_compound_dirty(item_id: str, *, immediate: bool = False) -> None
        self.on_compound_dirty = lambda item_id, *, immediate=False: None

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
            self.engine.set_outputs(self._show_outputs())

    def _show_outputs(self):
        if self.session is None:
            return []
        return list(self.session.show.settings.get("outputs", []))

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
    @staticmethod
    def _effective_output_id(placement, item):
        """Resolve the routing for a fire: a placement override wins; otherwise the
        item's default; None means the Default Output. Placement.output_id is None
        to inherit the item."""
        oid = getattr(placement, "output_id", None)
        if oid is None:
            oid = getattr(item, "output_id", None)
        return oid

    def _fire_placement(self, placement: CuePlacement) -> None:
        item = self._library_item(placement.library_item_id)
        if item is None:
            return

        if item.type == "stop":
            self._fire_stop(item)
            return

        if item.type == "fade":
            self._fire_fade(item)
            return

        pcm = load_cue_pcm(self.session, item)
        p = cue_engine_params(item)
        output_id = self._effective_output_id(placement, item)
        if item.background:
            self.engine.play_background(
                placement.id,
                pcm,
                gain_db=p["gain_db"],
                fade_in=p["fade_in"],
                loop=item.loop,
                fade_shape=p["fade_shape"],
                output_id=output_id,
            )
        else:  # "normal"
            self.engine.play_normal(
                placement.id,
                pcm,
                gain_db=p["gain_db"],
                fade_in=p["fade_in"],
                fade_out=p["fade_out"],
                fade_shape=p["fade_shape"],
                output_id=output_id,
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

    def _fire_fade(self, item: LibraryItem) -> None:
        db, secs, shape, stop = (
            item.fade_to_db, item.fade_time_seconds, item.fade_shape, item.fade_stop_when_done,
        )
        if item.fade_target == "allBackgrounds":
            self.engine.set_all_backgrounds_gain(db, secs, shape=shape, stop_when_done=stop)
            return
        # Specific library item: ramp every running voice (normal OR background)
        # whose placement references that item. The engine is keyed by placement
        # id, so resolve running voices back to placements.
        status = self.engine.get_status()
        normal = status.get("normal")
        if normal and not normal.get("finished"):
            pl = self._placement(normal["cue_id"])
            if pl is not None and pl.library_item_id == item.fade_target:
                self.engine.set_cue_gain(pl.id, db, secs, shape=shape, stop_when_done=stop)
        for bg in status.get("backgrounds", []):
            pl = self._placement(bg["cue_id"])
            if pl is not None and pl.library_item_id == item.fade_target:
                self.engine.set_cue_gain(pl.id, db, secs, shape=shape, stop_when_done=stop)

    # =====================================================================
    # Chain resolution (ADR 0001: resolve at GO into frame-offset schedules)
    # =====================================================================
    def _trimmed_frames(self, item: LibraryItem) -> Optional[int]:
        """End of a cue in frames, or None if its End is undefined (loops).

        For a stop cue the "length" is its fade time (0 when hard). For a normal/
        compound cue it is the trimmed audio length. A looping background cue has
        no End (loop is honored only for the background role -- ADR 0006).
        """
        if item.type == "stop":
            secs = item.stop_fade_seconds if item.stop_mode == "fade" else 0.0
            return seconds_to_frames(secs)
        if item.type == "fade":
            return seconds_to_frames(item.fade_time_seconds)   # End = start + fade_time
        if item.background and item.loop:
            return None
        end = item.trim_out if item.trim_out else item.duration
        return max(0, seconds_to_frames(end - item.trim_in))

    def _chain_from(
        self, seq: list[CuePlacement], start_index: int
    ) -> list[CuePlacement]:
        """The chain headed at ``seq[start_index]``: that placement plus every
        consecutive follower whose trigger mode is withPrevious/afterPrevious."""
        members = [seq[start_index]]
        i = start_index + 1
        while i < len(seq) and seq[i].trigger_mode in ("withPrevious", "afterPrevious"):
            members.append(seq[i])
            i += 1
        return members

    def _resolve_chain(self, members: list[CuePlacement]):
        """Resolve chain members to ``(placement, start_frames, kind)`` tuples.

        Uses ONLY model fields -- no PCM is loaded for the math. Returns the
        resolved list plus the count of members that are part of the chain (an
        ``afterPrevious`` behind a looping predecessor breaks it: that member and
        the rest fall out and become manual cues).
        """
        start_by_id: dict[str, int] = {}
        end_by_id: dict[str, Optional[int]] = {}
        resolved: list[tuple[CuePlacement, int, str]] = []
        for pos, m in enumerate(members):
            item = self._library_item(m.library_item_id)
            if pos == 0:
                start = seconds_to_frames(m.pre_wait)
            else:
                prev = members[pos - 1]
                if m.trigger_mode == "afterPrevious":
                    prev_end = end_by_id.get(prev.id)
                    if prev_end is None:
                        # Predecessor loops -> End undefined -> break the chain.
                        break
                    start = prev_end + seconds_to_frames(m.pre_wait)
                else:  # withPrevious
                    start = start_by_id[prev.id] + seconds_to_frames(m.pre_wait)
            start_by_id[m.id] = start
            trimmed = self._trimmed_frames(item) if item is not None else None
            end_by_id[m.id] = None if trimmed is None else start + trimmed
            resolved.append((m, start, item.type if item is not None else "normal"))
        return resolved, len(resolved)

    def _launch_chain(self, placement: CuePlacement, start_index: int) -> int:
        """Fire/arm a whole chain from one GO; return the cursor park index.

        The park index is one past the last chained member in the page sequence,
        so the cursor sits on the next manual cue.
        """
        seq = self._sequence(placement.page)
        members = self._chain_from(seq, start_index)
        # Re-firing a chain cancels its OWN pending followers and reschedules;
        # chains launched by other GOs are left running.
        for m in members:
            self.engine.cancel_scheduled(m.id)
        resolved, chained_count = self._resolve_chain(members)
        earlier: list[CuePlacement] = []
        for m, start_frames, _kind in resolved:
            if start_frames <= 0:
                self._fire_placement(m)          # live, unchanged path
            else:
                self._schedule_placement(m, start_frames, earlier)
            earlier.append(m)
        return start_index + chained_count

    def _schedule_placement(
        self,
        placement: CuePlacement,
        start_frames: int,
        chain_earlier: list[CuePlacement],
    ) -> None:
        """Arm a placement to fire ``start_frames`` from now (mirrors _fire_placement)."""
        item = self._library_item(placement.library_item_id)
        if item is None:
            return

        if item.type == "stop":
            if item.stop_target == "allBackgrounds":
                self.engine.schedule_stop_all_backgrounds(
                    placement.id,
                    start_frames,
                    mode=item.stop_mode,
                    fade_seconds=item.stop_fade_seconds,
                )
                return
            # Specific target: currently-running backgrounds of that item (at GO
            # time) PLUS earlier members of THIS chain that start the same item,
            # so a chain that starts a background and later chains a stop for it
            # catches its own. (A scheduled stop_background is a safe no-op if the
            # target is not live at activation.)
            candidates: set[str] = set()
            status = self.engine.get_status()
            for bg in status.get("backgrounds", []):
                pl = self._placement(bg["cue_id"])
                if pl is not None and pl.library_item_id == item.stop_target:
                    candidates.add(pl.id)
            for pl in chain_earlier:
                it = self._library_item(pl.library_item_id)
                if it is not None and it.background and \
                        pl.library_item_id == item.stop_target:
                    candidates.add(pl.id)
            for pid in candidates:
                self.engine.schedule_stop_background(
                    placement.id,
                    pid,
                    start_frames,
                    mode=item.stop_mode,
                    fade_seconds=item.stop_fade_seconds,
                )
            return

        if item.type == "fade":
            db, secs, shape, stop = (
                item.fade_to_db, item.fade_time_seconds, item.fade_shape, item.fade_stop_when_done,
            )
            if item.fade_target == "allBackgrounds":
                self.engine.schedule_fade(placement.id, ALL_BACKGROUNDS, start_frames, db, secs,
                                          shape=shape, stop_when_done=stop)
                return
            # Specific target: currently-running voices of that item (at GO time)
            # PLUS earlier members of THIS chain that start it. A scheduled
            # specific fade is a safe no-op if the target is not live at activation.
            candidates = set()
            status = self.engine.get_status()
            normal = status.get("normal")
            if normal and not normal.get("finished"):
                pl = self._placement(normal["cue_id"])
                if pl is not None and pl.library_item_id == item.fade_target:
                    candidates.add(pl.id)
            for bg in status.get("backgrounds", []):
                pl = self._placement(bg["cue_id"])
                if pl is not None and pl.library_item_id == item.fade_target:
                    candidates.add(pl.id)
            for pl in chain_earlier:
                it = self._library_item(pl.library_item_id)
                if it is not None and pl.library_item_id == item.fade_target and \
                        it.type in ("normal", "compound"):
                    candidates.add(pl.id)
            for pid in candidates:
                self.engine.schedule_fade(placement.id, pid, start_frames, db, secs,
                                          shape=shape, stop_when_done=stop)
            return

        pcm = load_cue_pcm(self.session, item)
        p = cue_engine_params(item)
        output_id = self._effective_output_id(placement, item)
        if item.background:
            self.engine.schedule_background(
                placement.id,
                pcm,
                start_frames,
                gain_db=p["gain_db"],
                fade_in=p["fade_in"],
                loop=item.loop,
                fade_shape=p["fade_shape"],
                output_id=output_id,
            )
        else:  # "normal"
            self.engine.schedule_normal(
                placement.id,
                pcm,
                start_frames,
                gain_db=p["gain_db"],
                fade_in=p["fade_in"],
                fade_out=p["fade_out"],
                fade_shape=p["fade_shape"],
                output_id=output_id,
            )

    def _predecessor_loops(self, placement: CuePlacement) -> bool:
        """True if the placement immediately before this one in its page loops."""
        seq = self._sequence(placement.page)
        idx = self._index_of(seq, placement.id)
        if idx is None or idx == 0:
            return False
        prev_item = self._library_item(seq[idx - 1].library_item_id)
        return bool(prev_item is not None and prev_item.background and prev_item.loop)

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
        end_index = self._launch_chain(seq[idx], idx)
        self.cursors[self.current_page] = min(end_index, len(seq))
        self.go_lock_until = self._now() + GO_LOCK_SECONDS

    def fire(self, placement_id: str) -> None:
        if self.session is None:
            return
        placement = self._placement(placement_id)
        if placement is None:
            return
        seq = self._sequence(placement.page)
        idx = self._index_of(seq, placement_id)
        if idx is None:
            # Placement not in the page sequence (shouldn't happen): fire alone.
            self._fire_placement(placement)
            return
        end_index = self._launch_chain(placement, idx)
        self.cursors[placement.page] = min(end_index, len(seq))

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

    def pause(self) -> None:
        self.engine.pause_all()

    def resume(self) -> None:
        self.engine.resume_all()

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
        # A compound whose timeline references the deleted source must re-render
        # (that clip now resolves to silence) rather than waiting for next open.
        dirty = [
            it.id
            for it in show.library.values()
            if it.type == "compound" and _timeline_references(it.timeline, library_item_id)
        ]
        self._autosave()
        for cid in dirty:
            self.on_compound_dirty(cid)

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
    def _live_apply_gain(self, item: LibraryItem) -> None:
        """Ramp any running show voice of this item's placements to its stored gain
        over ~50 ms. ``set_cue_gain`` is a safe no-op for placements not live."""
        for p in self.session.show.placements:
            if p.library_item_id == item.id:
                self.engine.set_cue_gain(p.id, item.gain_db, LIVE_GAIN_RAMP_SECONDS)
        if self._audition_item_id == item.id:
            self.engine.set_cue_gain("__audition__", item.gain_db, LIVE_GAIN_RAMP_SECONDS)

    def update_library_item(self, library_item_id: str, fields: dict) -> None:
        item = self._library_item(library_item_id)
        if item is None:
            return
        gain_changed = False
        for key, value in (fields or {}).items():
            attr = _FIELD_ALIASES.get(key, key)
            if attr in _ALLOWED_FIELDS:
                if attr == "background":
                    # Role flag is meaningful only for normal/compound; stop and
                    # fade cues force it False (ADR 0006).
                    if item.type in ("stop", "fade"):
                        continue
                    value = bool(value)
                if attr == "output_id":
                    value = str(value) if value else None   # "" / falsy -> Default Output
                if attr == "gain_db" and value != item.gain_db:
                    gain_changed = True
                setattr(item, attr, value)
        if gain_changed:
            self._live_apply_gain(item)
        self._autosave()

    def update_placement(self, placement_id: str, fields: dict) -> None:
        """Patch a placement's sequencing (trigger mode + pre-wait).

        Rejects an unknown trigger mode, and rejects ``afterPrevious`` when the
        predecessor in the page sequence loops (its End is undefined). Pre-wait is
        coerced to a non-negative float. Invalid values leave the field unchanged.
        """
        placement = self._placement(placement_id)
        if placement is None:
            return
        for key, value in (fields or {}).items():
            attr = _PLACEMENT_FIELD_ALIASES.get(key, key)
            if attr not in _ALLOWED_PLACEMENT_FIELDS:
                continue
            if attr == "trigger_mode":
                if value not in _TRIGGER_MODES:
                    continue
                if value == "afterPrevious" and self._predecessor_loops(placement):
                    continue
                placement.trigger_mode = value
            elif attr == "pre_wait":
                try:
                    placement.pre_wait = max(0.0, float(value))
                except (TypeError, ValueError):
                    continue
            elif attr == "output_id":
                placement.output_id = str(value) if value else None
        self._autosave()

    def duplicate_library_item(self, library_item_id: str) -> Optional[LibraryItem]:
        from cueforge.project import new_id

        item = self._library_item(library_item_id)
        if item is None:
            return None
        clone = replace(item, id=new_id(), name=f"{item.name} (copy)")
        if clone.timeline is not None:
            # dataclasses.replace shallow-copies: without this, a duplicated
            # compound would share its timeline dict with the original.
            clone.timeline = deepcopy(clone.timeline)
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

    def create_fade_cue(
        self,
        name: Optional[str] = None,
        page: Optional[str] = None,
        column: Optional[str] = None,
        row: Optional[int] = None,
    ) -> Optional[LibraryItem]:
        """Create an audio-less fade control cue (default: fade all backgrounds to
        0 dB over 3 s), optionally placing it in the same action."""
        if self.session is None:
            return None
        from cueforge.project import make_library_item

        item = make_library_item(
            name or "Fade",
            type="fade",
            fade_target="allBackgrounds",
            fade_to_db=0.0,
            fade_time_seconds=3.0,
            fade_stop_when_done=False,
        )
        self.session.show.library[item.id] = item
        if page is not None and column is not None and row is not None:
            self.session.show.placements.append(make_placement(item.id, page, column, int(row)))
        self._autosave()
        return item

    def create_compound_cue(
        self,
        name: Optional[str] = None,
        page: Optional[str] = None,
        column: Optional[str] = None,
        row: Optional[int] = None,
    ) -> Optional[LibraryItem]:
        """Create an empty compound cue (its own multi-track timeline), optionally
        placing it in the same action. It carries no audio until rendered."""
        if self.session is None:
            return None
        from cueforge.project import make_library_item

        item = make_library_item(
            name or "Compound",
            type="compound",
            timeline={"tracks": []},
            render_state="pending",
        )
        self.session.show.library[item.id] = item
        if page is not None and column is not None and row is not None:
            self.session.show.placements.append(
                make_placement(item.id, page, column, int(row))
            )
        self._autosave()
        return item

    def update_timeline(self, library_item_id: str, timeline: dict) -> None:
        """Replace a compound's timeline (sanitized) and schedule a debounced render."""
        item = self._library_item(library_item_id)
        if item is None or item.type != "compound":
            return
        item.timeline = _sanitize_timeline(timeline)   # ValueError -> error frame
        item.render_state = "pending"
        item.render_error = ""
        self._autosave()
        self.on_compound_dirty(item.id)                # debounced render

    def render_compound(self, library_item_id: str) -> None:
        """Force an immediate (undebounced) re-render of a compound cue."""
        item = self._library_item(library_item_id)
        if item is None or item.type != "compound":
            return
        item.render_state = "pending"
        item.render_error = ""
        self._autosave()
        self.on_compound_dirty(item.id, immediate=True)

    def normalize_item(self, library_item_id: str) -> None:
        item = self._library_item(library_item_id)
        if item is None or item.audio_hash is None:
            return
        normalize(self.session, item)  # autosaves internally
        self._live_apply_gain(item)

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
            output_id=item.output_id,
        )
        self._audition_item_id = library_item_id

    def stop_audition(self) -> None:
        self.engine.stop_audition()
        self._audition_item_id = None

    # =====================================================================
    # Named outputs (settings-panel Outputs list + Test button)
    # =====================================================================
    def set_outputs(self, outputs) -> None:
        """Replace the show's Outputs list (settings-panel style). Validates: id
        present + unique, name non-empty, channel >= 1, mono coerced to bool,
        device kept as a name string or None. Writes settings, reconfigures the hub,
        autosaves."""
        if self.session is None:
            return
        validated, seen = [], set()
        for o in (outputs or []):
            if not isinstance(o, dict):
                continue
            oid = o.get("id")
            name = (o.get("name") or "").strip()
            if not oid or oid in seen or not name:
                continue
            seen.add(oid)
            try:
                channel = max(1, int(o.get("channel", 1)))
            except (TypeError, ValueError):
                channel = 1
            device = o.get("device")
            validated.append({
                "id": str(oid),
                "name": name,
                "device": str(device) if device else None,
                "channel": channel,
                "mono": bool(o.get("mono", False)),
            })
        self.session.show.settings["outputs"] = validated
        self.engine.set_outputs(validated)
        self._autosave()

    def test_output(self, output_id) -> None:
        """Play the generated identification tone on ``output_id`` via the audition
        path (mono output -> single beep; stereo -> L then R)."""
        from cueforge.engine.tones import make_identification_tone
        outputs = self.session.show.settings.get("outputs", []) if self.session else []
        o = next((x for x in outputs if x.get("id") == output_id), None)
        mono = bool(o.get("mono")) if o else False
        self.engine.audition(make_identification_tone(mono), gain_db=0.0, output_id=output_id)

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
            "paused": mapped.get("paused", False),
            "scheduled": mapped.get("scheduled", []),
            "goLockRemainingMs": self.go_lock_remaining_ms(),
            "deviceOk": bool(status.get("device_ok", False)),
            "deviceChannels": int(status.get("output_channels", 2)),
            "outputs": mapped.get("outputs", []),
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
        if action == protocol.CREATE_FADE_CUE:
            return self.create_fade_cue(
                params.get("name"),
                params.get("page"),
                params.get("column"),
                params.get("row"),
            )
        if action == protocol.CREATE_COMPOUND:
            return self.create_compound_cue(
                params.get("name"),
                params.get("page"),
                params.get("column"),
                params.get("row"),
            )
        if action == protocol.UPDATE_TIMELINE:
            return self.update_timeline(params["itemId"], params.get("timeline", {}))
        if action == protocol.RENDER_COMPOUND:
            return self.render_compound(params["itemId"])
        if action == protocol.NORMALIZE_ITEM:
            return self.normalize_item(params["libraryItemId"])
        if action == protocol.AUDITION_ITEM:
            return self.audition_item(params["libraryItemId"])
        if action == protocol.STOP_AUDITION:
            return self.stop_audition()
        if action == protocol.UPDATE_PLACEMENT:
            return self.update_placement(
                params["placementId"], params.get("fields", {})
            )
        if action == protocol.PAUSE:
            return self.pause()
        if action == protocol.RESUME:
            return self.resume()
        if action == protocol.SET_OUTPUTS:
            return self.set_outputs(params.get("outputs", []))
        if action == protocol.TEST_OUTPUT:
            return self.test_output(params.get("outputId"))
        raise ValueError(f"unknown action: {action!r}")
