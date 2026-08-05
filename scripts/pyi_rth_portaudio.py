# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""PyInstaller runtime hook: point sounddevice at the bundled PortAudio.

sounddevice locates its native library with ``ctypes.util.find_library`` and
only then hands the result to ``ffi.dlopen``. On Linux ``find_library`` consults
``ldconfig`` (and, failing that, a compiler), so it looks *only* at libraries
installed system-wide -- it never searches ``LD_LIBRARY_PATH`` and therefore
never finds the copy PyInstaller unpacked into ``sys._MEIPASS``. On a machine
with no ``libportaudio2`` package installed it returns ``None`` and sounddevice
raises ``OSError: PortAudio library not found`` before the bundled file is ever
tried.

So resolve that one name ourselves when frozen, and defer to the normal lookup
for everything else (including a system PortAudio, if the bundle is missing one).

Windows and macOS need none of this: sounddevice ships those binaries inside its
own wheel, which PyInstaller collects as ordinary package data.
"""

import glob
import os
import sys


def _install() -> None:
    if not getattr(sys, "frozen", False) or not sys.platform.startswith("linux"):
        return
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if not bundle_dir:
        return

    import ctypes.util

    _find_library = ctypes.util.find_library

    def find_library(name):
        if name == "portaudio":
            # Match the real soname, whatever minor version got bundled.
            for cand in sorted(glob.glob(os.path.join(bundle_dir, "libportaudio.so*"))):
                if os.path.isfile(cand):
                    return cand
        return _find_library(name)

    ctypes.util.find_library = find_library


_install()
