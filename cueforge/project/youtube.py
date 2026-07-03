# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""Download YouTube (or any yt-dlp supported) audio for the import pipeline.

The heavy lifting of turning that download into a library item is done by the
existing :func:`cueforge.project.importer.import_audio` -- this module only has
to produce a local audio file on disk plus a human-friendly title. We download
the *bestaudio* stream (no video), so there is nothing to convert here; the
importer's ffmpeg decode step handles the final conversion to the engine format.

``download_audio`` is an async generator so the HTTP layer can stream progress:
it yields ``{"type": "progress", "percent": float}`` updates and finally a
``{"type": "done", "path": str, "title": str}`` event.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess

from ..ffmpeg_util import resolve_ffmpeg, wait_for_ffmpeg
from ..ytdlp_util import ensure_ytdlp, resolve_ytdlp

# yt-dlp --newline emits progress lines like: "[download]  42.3% of 5.00MiB ..."
_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")

# Run "yt-dlp -U" at most once per process (per app session).
_updated = False


class YouTubeError(Exception):
    """Raised when a URL is invalid or the download fails."""


def _looks_like_url(url: str) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith(("http://", "https://"))


async def ensure_updated() -> None:
    """Ensure yt-dlp is present (download the latest if missing) and self-update
    once per session. Best-effort: never fatal here -- a still-missing binary is
    surfaced as a clear error later by :func:`download_audio` via ensure_ytdlp.
    """
    global _updated
    # Always make sure a binary exists (cheap once present). Downloading it here
    # means it happens under the import's "updating" phase in the UI.
    try:
        await asyncio.to_thread(ensure_ytdlp)
    except Exception:
        pass
    if _updated:
        return
    _updated = True  # set first so a failing update never retries every import
    try:
        ytdlp = resolve_ytdlp()
        await asyncio.to_thread(
            subprocess.run,
            [ytdlp, "-U"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except Exception:
        pass


async def download_audio(url: str, dest_dir: str):
    """Download bestaudio into ``dest_dir``; yield progress then a done event.

    Yields ``{"type": "progress", "percent": float}`` while downloading and a
    final ``{"type": "done", "path": str, "title": str}``. Raises
    :class:`YouTubeError` on an invalid URL or a non-zero yt-dlp exit.
    """
    if not _looks_like_url(url):
        raise YouTubeError("Enter a valid http(s) URL.")

    # Resolve tools off the event loop: ensure_ytdlp() may download yt-dlp and
    # wait_for_ffmpeg() may block on the startup ffmpeg download -- neither must
    # run on the asyncio loop or the whole server freezes.
    def _resolve_tools():
        wait_for_ffmpeg()
        return ensure_ytdlp(), os.path.dirname(resolve_ffmpeg())

    ytdlp, ffmpeg_dir = await asyncio.to_thread(_resolve_tools)
    title_file = os.path.join(dest_dir, "_title.txt")

    cmd = [
        ytdlp,
        "-f", "bestaudio/best",
        "--no-playlist",
        "--newline",
        "--no-part",
        "--ffmpeg-location", ffmpeg_dir,
        "-o", os.path.join(dest_dir, "%(id)s.%(ext)s"),
        "--print-to-file", "%(title)s", title_file,
        url.strip(),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    tail: list[str] = []
    if proc.stdout is None:  # pragma: no cover - PIPE guarantees a stream
        raise YouTubeError("yt-dlp produced no output stream")
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            tail.append(line)
            if len(tail) > 20:
                tail.pop(0)
        m = _PROGRESS_RE.search(line)
        if m:
            try:
                yield {"type": "progress", "percent": float(m.group(1))}
            except ValueError:
                pass

    rc = await proc.wait()
    if rc != 0:
        detail = "\n".join(tail).strip() or f"yt-dlp exited with code {rc}"
        raise YouTubeError(detail)

    # Locate the produced audio file (ignore our title sidecar).
    produced = [
        os.path.join(dest_dir, name)
        for name in os.listdir(dest_dir)
        if os.path.join(dest_dir, name) != title_file
        and os.path.isfile(os.path.join(dest_dir, name))
    ]
    if not produced:
        raise YouTubeError("yt-dlp reported success but produced no audio file.")
    path = produced[0]

    title = ""
    try:
        with open(title_file, "r", encoding="utf-8") as fh:
            title = fh.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        pass
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]

    yield {"type": "done", "path": path, "title": title}
