# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""validate_project_name: a project name is a filename stem, never a path."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from cueforge.server.app import validate_project_name


@pytest.mark.parametrize("name", [
    "Demo",
    "My Show (2)",
    "Städte & Träume - Akt 1",
    "  padded  ",  # stripped, not rejected
    "console",     # contains a reserved word but is not one
])
def test_valid_names_pass(name):
    assert validate_project_name(name) == name.strip()


@pytest.mark.parametrize("name", [
    "",
    "   ",
    "..",
    "../evil",
    "..\\evil",
    "a/b",
    "a\\b",
    "a:b",
    'a"b',
    "a?b",
    "a*b",
    "a|b",
    "a<b>c",
    "trailing.",
    "ctrl\x01char",
    "CON",
    "con",
    "NUL.backup",
    "COM7",
])
def test_invalid_names_rejected(name):
    with pytest.raises(HTTPException) as exc:
        validate_project_name(name)
    assert exc.value.status_code == 400
