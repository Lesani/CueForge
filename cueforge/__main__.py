# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Lesani. See LICENSE for details.
"""``python -m cueforge`` entry point.

Kept as a thin, import-safe shim: the actual startup logic lives in
:mod:`cueforge.launcher` (``main()``), guarded here so importing this module
(e.g. as a PyInstaller entry script) never runs the launcher as a side effect.
"""

from cueforge.launcher import main

if __name__ == "__main__":
    main()
