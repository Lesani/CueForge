# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""ProjectSession: portable ``.cueforge`` files backed by a working folder.

A project is a single portable ``.cueforge`` file (a zip) containing
``show.json`` plus ``audio/<hash>.flac`` decoded audio. While open, CueForge
works from an extracted working folder; metadata (``show.json``) autosaves
continuously and audio is written once at import.
"""

from __future__ import annotations

import json
import os
import re
import zipfile

from .model import Show, make_show

SHOW_JSON = "show.json"
AUDIO_SUBDIR = "audio"

# Caps on an opened .cueforge archive (which may be an untrusted upload):
# far above any real show, but low enough that a crafted zip bomb cannot
# fill the disk. A 4-hour stereo 48 kHz show is ~2.7 GB decoded.
MAX_ARCHIVE_MEMBERS = 4096
MAX_ARCHIVE_BYTES = 16 * 1024**3

# Content-addressed audio blobs are named by their sha256. The hash may come
# from an untrusted show.json (uploaded .cueforge) and is joined into a path,
# so anything that is not a plain hex digest must be rejected.
_AUDIO_HASH_RE = re.compile(r"[0-9a-fA-F]{64}")


class ProjectSession:
    """An open project rooted at an extracted working folder."""

    def __init__(self, show: Show, work_dir: str) -> None:
        self._show = show
        self._work_dir = os.path.abspath(work_dir)
        os.makedirs(self._audio_dir, exist_ok=True)

    # -- properties --------------------------------------------------------

    @property
    def show(self) -> Show:
        return self._show

    @property
    def work_dir(self) -> str:
        return self._work_dir

    @property
    def audio_dir(self) -> str:
        return self._audio_dir

    @property
    def _audio_dir(self) -> str:
        return os.path.join(self._work_dir, AUDIO_SUBDIR)

    @property
    def _show_json_path(self) -> str:
        return os.path.join(self._work_dir, SHOW_JSON)

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def create_new(cls, work_dir: str, name: str) -> "ProjectSession":
        """Create a brand-new empty project in ``work_dir``."""
        os.makedirs(work_dir, exist_ok=True)
        show = make_show(name)
        session = cls(show, work_dir)
        session.autosave()
        return session

    @classmethod
    def open(cls, cueforge_path: str, work_dir: str) -> "ProjectSession":
        """Extract a ``.cueforge`` zip into ``work_dir`` and load the show.

        Only ``show.json`` and ``audio/*`` members are extracted, capped in
        count and total uncompressed size -- the archive may be an untrusted
        upload. Raises ``ValueError`` when a cap is exceeded.
        """
        os.makedirs(work_dir, exist_ok=True)
        with zipfile.ZipFile(cueforge_path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("project archive has too many entries")
            if sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError("project archive too large")
            members = [
                i.filename
                for i in infos
                if not i.is_dir()
                and (
                    i.filename == SHOW_JSON
                    or i.filename.startswith(f"{AUDIO_SUBDIR}/")
                )
            ]
            zf.extractall(work_dir, members=members)
        show_json = os.path.join(work_dir, SHOW_JSON)
        with open(show_json, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        show = Show.from_dict(data)
        return cls(show, work_dir)

    # -- persistence -------------------------------------------------------

    def autosave(self) -> None:
        """Write ``show.json`` into the working folder (call after mutations)."""
        os.makedirs(self._work_dir, exist_ok=True)
        data = self._show.to_dict()
        tmp = self._show_json_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self._show_json_path)

    def save_as(self, cueforge_path: str) -> None:
        """Repackage the working folder into a portable ``.cueforge`` zip."""
        self.autosave()
        cueforge_path = os.path.abspath(cueforge_path)
        parent = os.path.dirname(cueforge_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = cueforge_path + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(self._show_json_path, SHOW_JSON)
            audio_dir = self._audio_dir
            if os.path.isdir(audio_dir):
                for entry in sorted(os.listdir(audio_dir)):
                    full = os.path.join(audio_dir, entry)
                    if os.path.isfile(full):
                        zf.write(full, f"{AUDIO_SUBDIR}/{entry}")
        os.replace(tmp, cueforge_path)

    # -- audio storage helpers --------------------------------------------

    def audio_path(self, audio_hash: str) -> str:
        """Return the on-disk path for a content-addressed audio blob.

        Raises ``ValueError`` unless ``audio_hash`` is a sha256 hex digest --
        it may originate from an untrusted show.json and must never be able
        to path-traverse out of the audio folder.
        """
        if not _AUDIO_HASH_RE.fullmatch(str(audio_hash or "")):
            raise ValueError("invalid audio hash")
        return os.path.join(self._audio_dir, f"{audio_hash}.flac")

    def has_audio(self, audio_hash: str) -> bool:
        """Whether the decoded audio for ``audio_hash`` is already stored."""
        try:
            return os.path.isfile(self.audio_path(audio_hash))
        except ValueError:
            return False
