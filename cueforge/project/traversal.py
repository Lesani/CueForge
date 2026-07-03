# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Pure grid traversal helpers.

The GO order is COLUMN-MAJOR: for each column left-to-right, walk rows
top-to-bottom, including only cells that have a placement (empty cells skipped).
"""

from __future__ import annotations

from typing import Optional

from .model import CuePlacement, Page, Show


def _find_page(show: Show, page_id: str) -> Optional[Page]:
    for page in show.pages:
        if page.id == page_id:
            return page
    return None


def placement_at(
    show: Show, page_id: str, column_id: str, row: int
) -> Optional[CuePlacement]:
    """Return the placement at (page, column, row), or None if empty."""
    for p in show.placements:
        if p.page == page_id and p.column == column_id and p.row == row:
            return p
    return None


def page_cue_sequence(show: Show, page_id: str) -> list[CuePlacement]:
    """Return placements on a page in column-major GO order (gaps skipped)."""
    page = _find_page(show, page_id)
    if page is None:
        return []

    # Index placements for this page by (column, row) for O(1) lookup.
    by_cell: dict[tuple[str, int], CuePlacement] = {}
    for p in show.placements:
        if p.page == page_id:
            by_cell[(p.column, p.row)] = p

    sequence: list[CuePlacement] = []
    for column in page.columns:
        for row in range(column.rows):
            p = by_cell.get((column.id, row))
            if p is not None:
                sequence.append(p)
    return sequence
