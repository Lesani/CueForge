# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Import pipeline and content-address dedup.

Import computes a sha256 over the ORIGINAL source bytes. If a library item
already references those bytes, the import is a duplicate (the caller then
prompts use-existing vs clone). Otherwise ffmpeg decodes the source to the
engine format (48 kHz stereo float32), verifies it produced audio, stores it as
FLAC (content-addressed), and adds a new LibraryItem.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import soundfile as sf

from ..audio_format import CHANNELS, NP_DTYPE, SAMPLE_RATE
from ..ffmpeg_util import resolve_ffmpeg, wait_for_ffmpeg
from .model import LibraryItem, make_library_item
from .storage import ProjectSession


class ImportError(Exception):
    """Raised when a source file fails to decode and is rejected at import."""


# A decode that runs this long is a stuck/hostile input, not a real show file
# (a multi-hour album decodes in well under a minute).
DECODE_TIMEOUT = 600  # seconds


@dataclass
class ImportResult:
    """Outcome of an import attempt."""

    status: str  # "new" | "duplicate"
    audio_hash: str
    item: Optional[LibraryItem] = None
    matches: list[LibraryItem] = field(default_factory=list)


def _hash_file(path: str) -> str:
    """Return the sha256 hex digest of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _decode_to_pcm(src_path: str) -> np.ndarray:
    """Decode ``src_path`` via ffmpeg to (n, 2) float32 at the engine rate."""
    wait_for_ffmpeg()  # runs in a worker thread; waits out a startup download
    ffmpeg = resolve_ffmpeg()
    cmd = [
        ffmpeg,
        "-nostdin",
        "-v", "error",
        "-i", src_path,
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        "-f", "f32le",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=DECODE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ImportError(
            f"ffmpeg took longer than {DECODE_TIMEOUT}s to decode {src_path!r}"
        )
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise ImportError(f"ffmpeg failed to decode {src_path!r}: {msg}")
    pcm = np.frombuffer(proc.stdout, dtype=NP_DTYPE)
    if pcm.size == 0 or pcm.size % CHANNELS != 0:
        raise ImportError(f"decoded no usable audio from {src_path!r}")
    pcm = pcm.reshape(-1, CHANNELS)
    if pcm.shape[0] == 0:
        raise ImportError(f"decoded zero frames from {src_path!r}")
    return np.ascontiguousarray(pcm, dtype=NP_DTYPE)


def _find_matches(session: ProjectSession, audio_hash: str) -> list[LibraryItem]:
    """Return existing library items referencing ``audio_hash``."""
    return [
        item
        for item in session.show.library.values()
        if item.audio_hash == audio_hash
    ]


def import_audio(
    session: ProjectSession,
    src_path: str,
    *,
    name: Optional[str] = None,
) -> ImportResult:
    """Import an audio file: dedup, decode+store, and add a LibraryItem.

    Returns ``ImportResult(status="duplicate", ...)`` without mutating the show
    if the bytes already exist, else ``ImportResult(status="new", item=...)``.
    Raises :class:`ImportError` when the source fails to decode.
    """
    audio_hash = _hash_file(src_path)

    matches = _find_matches(session, audio_hash)
    if matches:
        return ImportResult(
            status="duplicate", audio_hash=audio_hash, matches=matches
        )

    # Decode + verify before storing anything. Write via a temp name so a
    # crash mid-write can never leave a truncated FLAC that has_audio() would
    # treat as valid forever.
    if not session.has_audio(audio_hash):
        pcm = _decode_to_pcm(src_path)
        final = session.audio_path(audio_hash)
        tmp = final + ".part"
        try:
            sf.write(tmp, pcm, SAMPLE_RATE, format="FLAC")
            os.replace(tmp, final)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    if name is None:
        base = os.path.basename(src_path)
        name = os.path.splitext(base)[0]

    duration = _audio_duration(session, audio_hash)
    item = make_library_item(
        name, type="normal", audio_hash=audio_hash, duration=duration
    )
    session.show.library[item.id] = item
    session.autosave()
    return ImportResult(status="new", audio_hash=audio_hash, item=item)


def _audio_duration(session: ProjectSession, audio_hash: str) -> float:
    """Full length in seconds of the stored decoded audio for ``audio_hash``."""
    info = sf.info(session.audio_path(audio_hash))
    return info.frames / info.samplerate if info.samplerate else 0.0


def add_clone(session: ProjectSession, audio_hash: str, name: str) -> LibraryItem:
    """Add a new LibraryItem referencing already-stored audio (own params)."""
    if not session.has_audio(audio_hash):
        raise ImportError(f"no stored audio for hash {audio_hash!r}")
    item = make_library_item(
        name,
        type="normal",
        audio_hash=audio_hash,
        duration=_audio_duration(session, audio_hash),
    )
    session.show.library[item.id] = item
    session.autosave()
    return item
