# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Tests for platform/arch identity.

These names decide which third-party binary gets downloaded and which release
asset the updater installs, so they are asserted against literals rather than
recomputed from the same helpers they are checking.
"""

import os

import pytest

from cueforge import platform_util


# ---------------------------------------------------------------- arch

@pytest.mark.parametrize(
    "machine, expected",
    [
        ("AMD64", "x86_64"),    # what Windows reports
        ("x86_64", "x86_64"),   # what Linux reports
        ("x64", "x86_64"),
        ("arm64", "aarch64"),   # what macOS/Windows report
        ("aarch64", "aarch64"),  # what Linux reports
        ("  X86_64  ", "x86_64"),  # tolerate padding/case
    ],
)
def test_arch_tag_normalises_cpu_names(machine, expected):
    assert platform_util.arch_tag(machine) == expected


def test_arch_tag_passes_through_unknown_names():
    # Better an obvious "no such asset" than a confidently wrong download.
    assert platform_util.arch_tag("riscv64") == "riscv64"


def test_arch_tag_defaults_to_this_machine():
    assert platform_util.arch_tag() in {"x86_64", "aarch64", "riscv64"}


# ---------------------------------------------------------------- assets

@pytest.mark.parametrize(
    "tag, expected",
    [
        # Windows keeps the historical name: updaters in already-released
        # builds look for exactly "CueForge.exe" and renaming it strands them.
        ("windows-x86_64", "CueForge.exe"),
        ("windows-aarch64", "CueForge.exe"),
        ("linux-x86_64", "CueForge-linux-x86_64"),
        ("linux-aarch64", "CueForge-linux-aarch64"),
    ],
)
def test_release_asset_name(tag, expected):
    assert platform_util.release_asset_name(tag) == expected


def test_release_asset_name_defaults_to_this_platform():
    assert platform_util.release_asset_name() == platform_util.release_asset_name(
        platform_util.platform_tag()
    )


def test_platform_tag_shape():
    tag = platform_util.platform_tag()
    system, _, arch = tag.partition("-")
    assert system in {"windows", "linux"} or system == platform_util.sys.platform
    assert arch == platform_util.arch_tag()


def test_exe_suffix_matches_platform():
    assert platform_util.EXE_SUFFIX == (".exe" if os.name == "nt" else "")


# ---------------------------------------------------------------- modes

@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes only")
def test_make_executable_sets_the_exec_bits(tmp_path):
    binary = tmp_path / "downloaded-tool"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o644)
    assert not os.access(binary, os.X_OK)

    platform_util.make_executable(str(binary))

    assert os.access(binary, os.X_OK)
    assert binary.stat().st_mode & 0o111 == 0o111


def test_make_executable_tolerates_a_missing_file(tmp_path):
    # Best-effort by contract; a genuinely unusable binary fails later at exec.
    platform_util.make_executable(str(tmp_path / "not-there"))
