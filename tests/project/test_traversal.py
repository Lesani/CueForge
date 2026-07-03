# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Traversal: column-major GO order with gaps skipped."""

from __future__ import annotations

from cueforge.project import model as m
from cueforge.project import traversal


def _grid():
    show = m.make_show("Show")
    c1 = m.make_column("Act I", 3)
    c2 = m.make_column("Act II", 3)
    page = m.make_page("P1", [c1, c2])
    show.pages.append(page)

    def place(col, row, label):
        item = m.make_library_item(label)
        show.library[item.id] = item
        p = m.make_placement(item.id, page.id, col.id, row)
        show.placements.append(p)
        return p

    # Column 1: rows 0,1,2 filled. Column 2: rows 0 and 2 filled (row 1 = gap).
    a = place(c1, 0, "A")
    b = place(c1, 1, "B")
    c = place(c1, 2, "C")
    d = place(c2, 0, "D")
    e = place(c2, 2, "E")
    return show, page, c1, c2, (a, b, c, d, e)


def test_column_major_skips_gap():
    show, page, c1, c2, (a, b, c, d, e) = _grid()
    seq = traversal.page_cue_sequence(show, page.id)
    # Column-major: c1 rows top->bottom, then c2; row1 of c2 skipped.
    assert [p.id for p in seq] == [a.id, b.id, c.id, d.id, e.id]


def test_placement_at():
    show, page, c1, c2, (a, b, c, d, e) = _grid()
    assert traversal.placement_at(show, page.id, c1.id, 1).id == b.id
    assert traversal.placement_at(show, page.id, c2.id, 1) is None
    assert traversal.placement_at(show, "nope", c1.id, 0) is None


def test_unknown_page_empty():
    show, page, c1, c2, _ = _grid()
    assert traversal.page_cue_sequence(show, "missing") == []
