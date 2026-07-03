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
    type: str = "normal"  # "normal" | "background" | "stop"
    audio_hash: Optional[str] = None
    duration: float = 0.0  # full decoded length in seconds (before trim); 0 for stop cues
    trim_in: float = 0.0
    trim_out: Optional[float] = None
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    fade_shape: str = "linear"  # "linear" | "equalPower"
    loop: bool = False
    # Flat, UI-only grouping label (no server-side hierarchy); "" = ungrouped.
    group: str = ""
    # stop-cue fields:
    stop_target: str = "allBackgrounds"  # "allBackgrounds" | <library item id>
    stop_mode: str = "hard"  # "hard" | "fade"
    stop_fade_seconds: float = 2.0
    # reserved for later (forward-compat, not implemented in MVP):
    auto_continue: object = None
    pre_wait: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "audioHash": self.audio_hash,
            "duration": self.duration,
            "trimIn": self.trim_in,
            "trimOut": self.trim_out,
            "gainDb": self.gain_db,
            "fadeIn": self.fade_in,
            "fadeOut": self.fade_out,
            "fadeShape": self.fade_shape,
            "loop": self.loop,
            "group": self.group,
            "stopTarget": self.stop_target,
            "stopMode": self.stop_mode,
            "stopFadeSeconds": self.stop_fade_seconds,
            "autoContinue": self.auto_continue,
            "preWait": self.pre_wait,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LibraryItem":
        return cls(
            id=d["id"],
            name=d["name"],
            type=d.get("type", "normal"),
            audio_hash=d.get("audioHash"),
            duration=d.get("duration", 0.0),
            trim_in=d.get("trimIn", 0.0),
            trim_out=d.get("trimOut"),
            gain_db=d.get("gainDb", 0.0),
            fade_in=d.get("fadeIn", 0.0),
            fade_out=d.get("fadeOut", 0.0),
            fade_shape=d.get("fadeShape", "linear"),
            loop=d.get("loop", False),
            # Legacy fallback: shows saved before the rename carry "folder".
            group=d.get("group", d.get("folder", "")),
            stop_target=d.get("stopTarget", "allBackgrounds"),
            stop_mode=d.get("stopMode", "hard"),
            stop_fade_seconds=d.get("stopFadeSeconds", 2.0),
            auto_continue=d.get("autoContinue"),
            pre_wait=d.get("preWait", 0.0),
        )


@dataclass
class CuePlacement:
    """A reference to a library item placed at a grid position."""

    id: str
    library_item_id: str
    page: str  # page id
    column: str  # column id
    row: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "libraryItemId": self.library_item_id,
            "page": self.page,
            "column": self.column,
            "row": self.row,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CuePlacement":
        return cls(
            id=d["id"],
            library_item_id=d["libraryItemId"],
            page=d["page"],
            column=d["column"],
            row=d["row"],
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
    id_factory: IdFactory = new_id,
) -> CuePlacement:
    """Create a CuePlacement with a generated id."""
    return CuePlacement(
        id=id_factory(),
        library_item_id=library_item_id,
        page=page,
        column=column,
        row=row,
    )


def make_show(name: str, *, id_factory: IdFactory = new_id) -> Show:
    """Create an empty Show with a generated id."""
    return Show(id=id_factory(), name=name)
