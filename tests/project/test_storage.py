# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""ProjectSession: round-trip save/open and continuous autosave."""

from __future__ import annotations

import json
import os
import zipfile

import pytest

from cueforge.project import importer
from cueforge.project import model as m
from cueforge.project import storage
from cueforge.project.storage import ProjectSession

from .conftest import write_tone_wav


def _build_show(session) -> None:
    show = session.show
    col = m.make_column("Act I", 4)
    page = m.make_page("Page 1", [col])
    show.pages.append(page)
    item = m.make_library_item("Tone", audio_hash="h1")
    show.library[item.id] = item
    show.placements.append(m.make_placement(item.id, page.id, col.id, 0))
    session.autosave()


def test_save_as_open_roundtrip(tmp_path):
    work1 = tmp_path / "work1"
    session = ProjectSession.create_new(str(work1), "MyShow")
    src = str(tmp_path / "tone.wav")
    write_tone_wav(src, seconds=0.5)
    importer.import_audio(session, src)
    _build_show(session)
    before = session.show.to_dict()

    pkg = str(tmp_path / "MyShow.cueforge")
    session.save_as(pkg)
    assert os.path.isfile(pkg)

    work2 = tmp_path / "work2"
    reopened = ProjectSession.open(pkg, str(work2))
    assert reopened.show.to_dict() == before
    # Audio blob travelled inside the zip and was extracted.
    audio_files = os.listdir(reopened.audio_dir)
    assert any(f.endswith(".flac") for f in audio_files)


def test_autosave_reflects_mutation(tmp_path):
    work = tmp_path / "work"
    session = ProjectSession.create_new(str(work), "MyShow")
    session.show.name = "Renamed"
    session.show.settings["port"] = 7070
    session.autosave()

    with open(os.path.join(str(work), "show.json"), "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["name"] == "Renamed"
    assert data["settings"]["port"] == 7070


class TestAudioHashValidation:
    """audio_hash may come from an untrusted show.json -- never a path."""

    VALID = "a" * 64

    def test_valid_hash_builds_path(self, tmp_path):
        session = ProjectSession.create_new(str(tmp_path / "w"), "S")
        path = session.audio_path(self.VALID)
        assert path.endswith(f"{self.VALID}.flac")

    @pytest.mark.parametrize("bad", [
        "",
        None,
        "../../../etc/passwd",
        "..\\..\\evil",
        "a" * 63,
        "a" * 65,
        "g" * 64,               # not hex
        "a" * 60 + "/../x",
    ])
    def test_invalid_hash_raises(self, tmp_path, bad):
        session = ProjectSession.create_new(str(tmp_path / "w"), "S")
        with pytest.raises(ValueError):
            session.audio_path(bad)
        assert session.has_audio(bad) is False


class TestOpenArchiveGuards:
    def _make_zip(self, path, entries):
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries:
                zf.writestr(name, data)

    def test_only_show_and_audio_members_extracted(self, tmp_path):
        pkg = str(tmp_path / "p.cueforge")
        show = ProjectSession.create_new(str(tmp_path / "seed"), "S").show
        self._make_zip(pkg, [
            ("show.json", json.dumps(show.to_dict())),
            ("audio/" + "b" * 64 + ".flac", b"x"),
            ("rogue.txt", b"nope"),
        ])
        work = tmp_path / "work"
        ProjectSession.open(pkg, str(work))
        assert (work / "audio" / ("b" * 64 + ".flac")).is_file()
        assert not (work / "rogue.txt").exists()

    def test_too_many_members_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "MAX_ARCHIVE_MEMBERS", 3)
        pkg = str(tmp_path / "p.cueforge")
        self._make_zip(pkg, [(f"audio/{i}.flac", b"") for i in range(4)])
        with pytest.raises(ValueError, match="too many"):
            ProjectSession.open(pkg, str(tmp_path / "work"))

    def test_oversized_archive_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "MAX_ARCHIVE_BYTES", 10)
        pkg = str(tmp_path / "p.cueforge")
        self._make_zip(pkg, [("audio/a.flac", b"y" * 100)])
        with pytest.raises(ValueError, match="too large"):
            ProjectSession.open(pkg, str(tmp_path / "work"))
