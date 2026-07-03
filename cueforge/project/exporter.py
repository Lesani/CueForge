# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Transcode stored cue audio for download / decode fallback.

Two jobs, both shelling out to the vendored ffmpeg (see
:func:`cueforge.ffmpeg_util.resolve_ffmpeg`):

* :func:`transcode_to_wav` -- straight decode of a stored FLAC to 16-bit PCM WAV
  (kept 48 kHz stereo). iOS Safari's WebAudio ``decodeAudioData`` cannot decode
  FLAC, so remote iPhones/iPads fetch this instead for waveform + audition.
* :func:`export_cue` -- render a library item to wav/flac/mp3 with its cue
  parameters (trim, gain, fades) baked in, for the "export cue" download.

Both run ffmpeg synchronously; callers on the event loop should wrap them in
``asyncio.to_thread`` so they never block the reactor.
"""

from __future__ import annotations

import subprocess
import urllib.parse

from ..audio_format import CHANNELS, SAMPLE_RATE
from ..ffmpeg_util import resolve_ffmpeg, wait_for_ffmpeg
from .model import LibraryItem
from .storage import ProjectSession

# Export container/codec choices. mp3 uses libmp3lame (verified present in the
# vendored ffmpeg build); wav is 16-bit PCM; flac is the default flac encoder.
EXPORT_FORMATS = ("wav", "flac", "mp3")

_CODEC_ARGS = {
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
}

_MEDIA_TYPES = {
    "wav": "audio/wav",
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
}


class ExportError(Exception):
    """Raised when a transcode fails or an item cannot be exported."""


def media_type_for(fmt: str) -> str:
    """Return the HTTP media type for an export format."""
    return _MEDIA_TYPES.get(fmt, "application/octet-stream")


def _run_ffmpeg(cmd: list[str]) -> None:
    """Run ffmpeg, raising :class:`ExportError` with stderr on failure."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip()
        raise ExportError(f"ffmpeg failed: {msg}")


def transcode_to_wav(src_path: str, dst_path: str) -> None:
    """Decode ``src_path`` (stored FLAC) to 16-bit PCM WAV at ``dst_path``.

    Keeps the engine format (48 kHz stereo); only the sample encoding changes.
    """
    wait_for_ffmpeg()  # worker thread; waits out a startup download
    ffmpeg = resolve_ffmpeg()
    cmd = [
        ffmpeg,
        "-nostdin",
        "-v", "error",
        "-y",
        "-i", src_path,
        "-ar", str(SAMPLE_RATE),
        "-ac", str(CHANNELS),
        "-c:a", "pcm_s16le",
        dst_path,
    ]
    _run_ffmpeg(cmd)


def export_cue(
    session: ProjectSession,
    item: LibraryItem,
    fmt: str,
    dst_path: str,
) -> None:
    """Render ``item`` to ``dst_path`` in ``fmt`` with its cue params applied.

    Applies trim (``trim_in``/``trim_out``), ``gain_db`` (volume filter) and
    ``fade_in``/``fade_out`` (afade). The fade-out starts at
    ``trimmed_duration - fade_out``. Loop is intentionally NOT applied. Raises
    :class:`ExportError` for a bad format or an item with no audio.
    """
    if fmt not in EXPORT_FORMATS:
        raise ExportError(f"unsupported export format: {fmt!r}")
    if item.type == "stop" or item.audio_hash is None:
        raise ExportError("item has no audio to export")

    src_path = session.audio_path(item.audio_hash)

    trim_in = max(0.0, float(item.trim_in or 0.0))
    # Effective end of the exported region: explicit trim_out, else full length.
    if item.trim_out is not None:
        trim_out = float(item.trim_out)
    else:
        trim_out = float(item.duration or 0.0)
    trimmed_duration = max(0.0, trim_out - trim_in)

    wait_for_ffmpeg()  # worker thread; waits out a startup download
    ffmpeg = resolve_ffmpeg()
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-y"]
    # Input seeking resets timestamps to zero, so afade start times below are
    # relative to the trimmed region.
    if trim_in > 0.0:
        cmd += ["-ss", f"{trim_in:.6f}"]
    cmd += ["-i", src_path]
    # Only limit the output length when an explicit trim_out was set; a bare
    # trim_in keeps everything after the seek point.
    if item.trim_out is not None and trimmed_duration > 0.0:
        cmd += ["-t", f"{trimmed_duration:.6f}"]

    filters: list[str] = []
    if item.gain_db:
        filters.append(f"volume={float(item.gain_db):.6f}dB")
    fade_in = float(item.fade_in or 0.0)
    if fade_in > 0.0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.6f}")
    fade_out = float(item.fade_out or 0.0)
    if fade_out > 0.0 and trimmed_duration > 0.0:
        start = max(0.0, trimmed_duration - fade_out)
        filters.append(f"afade=t=out:st={start:.6f}:d={fade_out:.6f}")
    if filters:
        cmd += ["-af", ",".join(filters)]

    cmd += _CODEC_ARGS[fmt]
    cmd += [dst_path]
    _run_ffmpeg(cmd)


def _ascii_filename(name: str) -> str:
    """Reduce ``name`` to an ASCII-safe base (letters/digits/space/-/_/.)."""
    safe = "".join(
        c if (c.isalnum() and c.isascii()) or c in " -_." else "_" for c in name
    ).strip()
    return safe or "cue"


def content_disposition(name: str, ext: str) -> str:
    """Build an attachment Content-Disposition with an ASCII + UTF-8 filename.

    The ASCII ``filename="..."`` is a safe fallback; ``filename*=UTF-8''...``
    (RFC 5987) preserves umlauts etc. for clients that understand it.
    """
    ascii_name = f"{_ascii_filename(name)}.{ext}"
    utf8_name = f"{name}.{ext}"
    quoted = urllib.parse.quote(utf8_name, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"
