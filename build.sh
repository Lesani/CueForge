#!/usr/bin/env sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
# ---------------------------------------------------------------------------
# Build the CueForge binary reliably (the build.bat counterpart).
#
# PyInstaller runs with --clean/--noconfirm, which deletes dist/ first. A
# running instance holding files open there can leave the delete half-done and
# the bundle corrupted (the binary then serves {"web":"no index.html yet"} or a
# "half" page when a bundled JS module 404s). So always stop any running
# instance before building.
# ---------------------------------------------------------------------------
set -eu

cd "$(dirname "$0")"

ASSET=$(python3 -c 'from cueforge.platform_util import release_asset_name; print(release_asset_name())' 2>/dev/null || echo CueForge)

echo "[build] Stopping any running $ASSET..."
pkill -f "dist/$ASSET" 2>/dev/null || true
sleep 1

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "[build] Building with $PY ..."
if "$PY" build_exe.py; then
  echo
  echo "[build] Done -> dist/$ASSET"
else
  RC=$?
  echo
  echo "[build] FAILED with exit code $RC. See the output above." >&2
  exit "$RC"
fi
