# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Unit tests for cueforge.update_util -- the pure/offline parts only.

Network calls (check_now, _download) and the frozen-exe gate are exercised
manually; here we cover version comparison, release-JSON extraction, checksum
parsing, and the rename-swap that replaces the running exe.
"""

import pytest

from cueforge import update_util


# --------------------------------------------------------------------------
# parse_version / is_newer
# --------------------------------------------------------------------------
class TestVersionCompare:
    def test_parse_strips_v_prefix_and_suffix(self):
        assert update_util.parse_version("v0.2.10") == (0, 2, 10)
        assert update_util.parse_version("1.0.0-rc1") == (1, 0, 0)

    def test_parse_garbage_is_none(self):
        assert update_util.parse_version("") is None
        assert update_util.parse_version("nightly") is None
        assert update_util.parse_version(None) is None

    @pytest.mark.parametrize(
        "latest,current,expected",
        [
            ("0.2.0", "0.1.0", True),
            ("v0.2.0", "0.1.0", True),
            ("0.1.0", "0.1.0", False),
            ("0.1.0", "0.2.0", False),
            ("0.1.10", "0.1.9", True),   # numeric, not lexicographic
            ("0.2", "0.2.0", False),     # padded comparison, both directions
            ("0.2.0", "0.2", False),
            ("0.2.1", "0.2", True),
            (None, "0.1.0", False),      # unknown never nags
            ("0.2.0", None, False),
            ("junk", "0.1.0", False),
        ],
    )
    def test_is_newer(self, latest, current, expected):
        assert update_util.is_newer(latest, current) is expected


# --------------------------------------------------------------------------
# _extract_release
# --------------------------------------------------------------------------
# One release carries every platform's build, so the asset this process looks
# for depends on where it is running.
OUR_ASSET = update_util.ASSET_NAME

#: Every asset a release publishes, in the shape the GitHub API returns.
ALL_ASSETS = [
    {
        "name": name,
        "browser_download_url": f"https://example.test/{name}",
        "size": size,
    }
    for name, size in (
        ("CueForge.exe", 12345),
        ("CueForge-linux-x86_64", 23456),
        ("CueForge-linux-aarch64", 34567),
    )
]

SAMPLE_RELEASE = {
    "tag_name": "v0.2.0",
    "html_url": "https://github.com/Lesani/CueForge/releases/tag/v0.2.0",
    "assets": ALL_ASSETS + [
        {
            "name": "SHA256SUMS.txt",
            "browser_download_url": "https://example.test/SHA256SUMS.txt",
            "size": 78,
        },
    ],
}


class TestExtractRelease:
    def test_full_release(self):
        info = update_util._extract_release(SAMPLE_RELEASE)
        assert info["latest"] == "0.2.0"
        assert info["url"].endswith("/v0.2.0")
        assert info["checksumsUrl"] == "https://example.test/SHA256SUMS.txt"

    def test_picks_the_asset_for_this_platform(self):
        # The build must ignore the other platforms' assets in the same release.
        info = update_util._extract_release(SAMPLE_RELEASE)
        assert info["assetUrl"] == f"https://example.test/{OUR_ASSET}"
        expected_size = next(a["size"] for a in ALL_ASSETS if a["name"] == OUR_ASSET)
        assert info["assetSize"] == expected_size

    def test_release_without_our_platforms_asset(self):
        others = [a for a in SAMPLE_RELEASE["assets"] if a["name"] != OUR_ASSET]
        info = update_util._extract_release({"tag_name": "v0.3.0", "assets": others})
        assert info["latest"] == "0.3.0"
        assert info["assetUrl"] is None

    def test_release_without_assets(self):
        info = update_util._extract_release({"tag_name": "v0.3.0", "assets": []})
        assert info["latest"] == "0.3.0"
        assert info["assetUrl"] is None
        assert info["checksumsUrl"] is None

    def test_empty_release(self):
        info = update_util._extract_release({})
        assert info["latest"] is None
        assert info["assetUrl"] is None


# --------------------------------------------------------------------------
# parse_checksums
# --------------------------------------------------------------------------
class TestParseChecksums:
    def test_sha256sum_format(self):
        digest = "a" * 64
        sums = update_util.parse_checksums(f"{digest}  CueForge.exe\n")
        assert sums == {"CueForge.exe": digest}

    def test_binary_marker_and_case(self):
        digest = "AB" * 32
        sums = update_util.parse_checksums(f"{digest} *CueForge.exe")
        assert sums == {"CueForge.exe": digest.lower()}

    def test_junk_lines_ignored(self):
        text = "not a checksum line\nxyz  file.exe\n\n"
        assert update_util.parse_checksums(text) == {}
        assert update_util.parse_checksums("") == {}


# --------------------------------------------------------------------------
# _swap_exe (rename dance, plain files -- no running process involved)
# --------------------------------------------------------------------------
class TestSwapExe:
    def test_swap_replaces_exe_and_keeps_old(self, tmp_path):
        exe = tmp_path / "CueForge.exe"
        new = tmp_path / "CueForge.exe.new"
        exe.write_bytes(b"old build")
        new.write_bytes(b"new build")

        update_util._swap_exe(str(exe), str(new))

        assert exe.read_bytes() == b"new build"
        assert (tmp_path / "CueForge.exe.old").read_bytes() == b"old build"
        assert not new.exists()

    def test_stale_old_file_is_replaced(self, tmp_path):
        exe = tmp_path / "CueForge.exe"
        new = tmp_path / "CueForge.exe.new"
        old = tmp_path / "CueForge.exe.old"
        exe.write_bytes(b"current")
        new.write_bytes(b"incoming")
        old.write_bytes(b"ancient leftover")

        update_util._swap_exe(str(exe), str(new))

        assert exe.read_bytes() == b"incoming"
        assert old.read_bytes() == b"current"

    def test_missing_download_rolls_back(self, tmp_path):
        exe = tmp_path / "CueForge.exe"
        exe.write_bytes(b"current")
        missing = tmp_path / "CueForge.exe.new"  # never created

        with pytest.raises(OSError):
            update_util._swap_exe(str(exe), str(missing))

        # the running install must remain intact under its original name
        assert exe.read_bytes() == b"current"
        assert not (tmp_path / "CueForge.exe.old").exists()


# --------------------------------------------------------------------------
# update_available (state-driven)
# --------------------------------------------------------------------------
class TestUpdateAvailable:
    def test_reflects_check_info(self, monkeypatch):
        monkeypatch.setitem(update_util._check_info, "current", "0.1.0")
        monkeypatch.setitem(update_util._check_info, "latest", "0.2.0")
        assert update_util.update_available() is True

        monkeypatch.setitem(update_util._check_info, "latest", "0.1.0")
        assert update_util.update_available() is False

        monkeypatch.setitem(update_util._check_info, "latest", None)
        assert update_util.update_available() is False
