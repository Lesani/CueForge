# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Project & library model + storage package.

Public API for building shows, storing them as portable ``.cueforge`` files,
importing/deduping audio, rendering cues to PCM, and grid traversal.
"""

from .importer import ImportError, ImportResult, add_clone, import_audio
from .model import (
    Column,
    CuePlacement,
    LibraryItem,
    Page,
    Show,
    make_column,
    make_library_item,
    make_page,
    make_placement,
    make_show,
    new_id,
)
from .render import cue_engine_params, load_cue_pcm, normalize
from .storage import ProjectSession
from .traversal import page_cue_sequence, placement_at

__all__ = [
    "Column",
    "CuePlacement",
    "LibraryItem",
    "Page",
    "Show",
    "make_column",
    "make_library_item",
    "make_page",
    "make_placement",
    "make_show",
    "new_id",
    "ProjectSession",
    "ImportError",
    "ImportResult",
    "import_audio",
    "add_clone",
    "load_cue_pcm",
    "cue_engine_params",
    "normalize",
    "page_cue_sequence",
    "placement_at",
]
