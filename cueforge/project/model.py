# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Data model for CueForge shows: library items, placements, grid, and Show.

All dataclasses are JSON-serializable via ``to_dict()`` / ``from_dict()`` so the
whole tree can be persisted to ``show.json``. IDs are uuid4 hex strings; helpers
accept an ``id_factory`` argument so callers/tests can be deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import uuid4


# show.json format version. Bump when the schema changes incompatibly and add
# a migration in ``Show.from_dict``; files without the key are version 1.
FORMAT_VERSION = 1


def new_id() -> str:
    """Return a fresh unique id (uuid4 hex)."""
    return uuid4().hex


# Type alias for an id-generating callable.
IdFactory = Callable[[], str]


@dataclass
class LibraryItem:
    """An imported, decoded sound plus its reusable parameters.

    ``audio_hash`` is the content hash of the stored decoded audio, or ``None``
    for ``stop`` cues (which carry no audio).
    """

    id: str
    name: str
    type: str = "normal"  # "normal" | "compound" | "stop" | "fade" (meta type, immutable)
    # Role flag: True = stackable/loopable background layer, False = exclusive
    # normal channel. Meaningful for type normal|compound only; forced False for
    # stop|fade. Replaces the old type=="background" (ADR 0006).
    background: bool = False
    audio_hash: Optional[str] = None
    duration: float = 0.0  # full decoded length in seconds (before trim); 0 for stop cues
    trim_in: float = 0.0
    trim_out: Optional[float] = None
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    fade_shape: str = "linear"  # "linear" | "equalPower"
    loop: bool = False
    # Named-output routing: the id of a defined Output (see show.settings.outputs)
    # this item plays into by default. None = the Default Output (channels 1-2).
    output_id: Optional[str] = None
    # Flat, UI-only grouping label (no server-side hierarchy); "" = ungrouped.
    group: str = ""
    # stop-cue fields:
    stop_target: str = "allBackgrounds"  # "allBackgrounds" | <library item id>
    stop_mode: str = "hard"  # "hard" | "fade"
    stop_fade_seconds: float = 2.0
    # fade-cue fields (fade_shape above doubles as the fade-cue ramp shape):
    fade_target: str = "allBackgrounds"  # "allBackgrounds" | <library item id>
    fade_to_db: float = 0.0              # target live gain in dB (replaces current)
    fade_time_seconds: float = 3.0       # ramp duration, > 0
    fade_stop_when_done: bool = False
    # compound-cue fields (type == "compound"):
    timeline: Optional[dict] = None          # {"tracks":[{id,name,gainDb,mute,clips:[...]}]}
    render_signature: str = ""               # hash of timeline + source audio hashes at last render
    render_state: str = ""                   # "" | "pending" | "rendering" | "ready" | "error"
    render_error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "background": self.background,
            "audioHash": self.audio_hash,
            "duration": self.duration,
            "trimIn": self.trim_in,
            "trimOut": self.trim_out,
            "gainDb": self.gain_db,
            "fadeIn": self.fade_in,
            "fadeOut": self.fade_out,
            "fadeShape": self.fade_shape,
            "loop": self.loop,
            "outputId": self.output_id,
            "group": self.group,
            "stopTarget": self.stop_target,
            "stopMode": self.stop_mode,
            "stopFadeSeconds": self.stop_fade_seconds,
            "fadeTarget": self.fade_target,
            "fadeToDb": self.fade_to_db,
            "fadeTimeSeconds": self.fade_time_seconds,
            "fadeStopWhenDone": self.fade_stop_when_done,
            "timeline": self.timeline,
            "renderSignature": self.render_signature,
            "renderState": self.render_state,
            "renderError": self.render_error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LibraryItem":
        # Legacy migration (ADR 0006, tolerant load, no FORMAT_VERSION bump):
        # the old type=="background" becomes type="normal" with the role flag set.
        raw_type = d.get("type", "normal")
        background = bool(d.get("background", False))
        if raw_type == "background":
            raw_type = "normal"
            background = True
        # The role is meaningless for stop/fade meta types; force it False even
        # if dirty data carries a stray flag.
        if raw_type in ("stop", "fade"):
            background = False
        return cls(
            id=d["id"],
            name=d["name"],
            type=raw_type,
            background=background,
            audio_hash=d.get("audioHash"),
            duration=d.get("duration", 0.0),
            trim_in=d.get("trimIn", 0.0),
            trim_out=d.get("trimOut"),
            gain_db=d.get("gainDb", 0.0),
            fade_in=d.get("fadeIn", 0.0),
            fade_out=d.get("fadeOut", 0.0),
            fade_shape=d.get("fadeShape", "linear"),
            loop=d.get("loop", False),
            output_id=d.get("outputId"),
            # Legacy fallback: shows saved before the rename carry "folder".
            group=d.get("group", d.get("folder", "")),
            stop_target=d.get("stopTarget", "allBackgrounds"),
            stop_mode=d.get("stopMode", "hard"),
            stop_fade_seconds=d.get("stopFadeSeconds", 2.0),
            fade_target=d.get("fadeTarget", "allBackgrounds"),
            fade_to_db=d.get("fadeToDb", 0.0),
            fade_time_seconds=d.get("fadeTimeSeconds", 3.0),
            fade_stop_when_done=d.get("fadeStopWhenDone", False),
            timeline=d.get("timeline"),
            render_signature=d.get("renderSignature", ""),
            render_state=d.get("renderState", ""),
            render_error=d.get("renderError", ""),
        )


@dataclass
class CuePlacement:
    """A reference to a library item placed at a grid position."""

    id: str
    library_item_id: str
    page: str  # page id
    column: str  # column id
    row: int
    # Sequencing: how this placement starts relative to its predecessor, and a
    # delay from that trigger to audio start. See CONTEXT.md ("Trigger Mode",
    # "Pre-Wait", "Chain").
    trigger_mode: str = "onTrigger"  # "onTrigger" | "withPrevious" | "afterPrevious"
    pre_wait: float = 0.0            # seconds, >= 0
    # Named-output override: an Output id that wins over the library item's
    # default. None inherits the item (which itself may be the Default Output).
    output_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "libraryItemId": self.library_item_id,
            "page": self.page,
            "column": self.column,
            "row": self.row,
            "triggerMode": self.trigger_mode,
            "preWait": self.pre_wait,
            "outputId": self.output_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CuePlacement":
        return cls(
            id=d["id"],
            library_item_id=d["libraryItemId"],
            page=d["page"],
            column=d["column"],
            row=d["row"],
            trigger_mode=d.get("triggerMode", "onTrigger"),
            pre_wait=d.get("preWait", 0.0),
            output_id=d.get("outputId"),
        )


@dataclass
class Column:
    """A named vertical column (Act) within a page."""

    id: str
    name: str
    rows: int

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "rows": self.rows}

    @classmethod
    def from_dict(cls, d: dict) -> "Column":
        return cls(id=d["id"], name=d["name"], rows=d["rows"])


@dataclass
class Page:
    """A screen of the grid, holding an ordered list of columns."""

    id: str
    name: str
    columns: list[Column] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "columns": [c.to_dict() for c in self.columns],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Page":
        return cls(
            id=d["id"],
            name=d["name"],
            columns=[Column.from_dict(c) for c in d.get("columns", [])],
        )


@dataclass
class Show:
    """The whole show: pages/grid, library, placements, and settings."""

    id: str
    name: str
    pages: list[Page] = field(default_factory=list)
    library: dict[str, LibraryItem] = field(default_factory=dict)
    placements: list[CuePlacement] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    format_version: int = FORMAT_VERSION

    def to_dict(self) -> dict:
        return {
            "formatVersion": self.format_version,
            "id": self.id,
            "name": self.name,
            "pages": [p.to_dict() for p in self.pages],
            "library": {k: v.to_dict() for k, v in self.library.items()},
            "placements": [p.to_dict() for p in self.placements],
            "settings": self.settings,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Show":
        version = int(d.get("formatVersion") or 1)
        if version > FORMAT_VERSION:
            raise ValueError(
                f"this show uses format version {version}, but this CueForge "
                f"only understands up to {FORMAT_VERSION} -- please update CueForge"
            )
        # version < FORMAT_VERSION: migrate here when the format ever changes.
        return cls(
            format_version=FORMAT_VERSION,
            id=d["id"],
            name=d["name"],
            pages=[Page.from_dict(p) for p in d.get("pages", [])],
            library={
                k: LibraryItem.from_dict(v)
                for k, v in d.get("library", {}).items()
            },
            placements=[
                CuePlacement.from_dict(p) for p in d.get("placements", [])
            ],
            settings=d.get("settings", {}),
        )


# ---------------------------------------------------------------------------
# Construction helpers (deterministic when given an id_factory)
# ---------------------------------------------------------------------------


def make_library_item(
    name: str,
    *,
    type: str = "normal",
    audio_hash: Optional[str] = None,
    id_factory: IdFactory = new_id,
    **kwargs,
) -> LibraryItem:
    """Create a LibraryItem with a generated id."""
    return LibraryItem(
        id=id_factory(),
        name=name,
        type=type,
        audio_hash=audio_hash,
        **kwargs,
    )


def make_column(name: str, rows: int, *, id_factory: IdFactory = new_id) -> Column:
    """Create a Column with a generated id."""
    return Column(id=id_factory(), name=name, rows=rows)


def make_page(
    name: str,
    columns: Optional[list[Column]] = None,
    *,
    id_factory: IdFactory = new_id,
) -> Page:
    """Create a Page with a generated id."""
    return Page(id=id_factory(), name=name, columns=list(columns or []))


def make_placement(
    library_item_id: str,
    page: str,
    column: str,
    row: int,
    *,
    trigger_mode: str = "onTrigger",
    pre_wait: float = 0.0,
    id_factory: IdFactory = new_id,
) -> CuePlacement:
    """Create a CuePlacement with a generated id."""
    return CuePlacement(
        id=id_factory(),
        library_item_id=library_item_id,
        page=page,
        column=column,
        row=row,
        trigger_mode=trigger_mode,
        pre_wait=pre_wait,
    )


def make_show(name: str, *, id_factory: IdFactory = new_id) -> Show:
    """Create an empty Show with a generated id."""
    return Show(id=id_factory(), name=name)
