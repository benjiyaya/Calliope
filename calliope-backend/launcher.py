"""PyInstaller entry point for the Calliope backend.

Why this exists: using src/calliope/main.py directly as the frozen entry script
makes PyInstaller bundle it as a TOP-LEVEL module named "main", so its __file__
becomes <_MEIPASS>/main.pyc and main.py's __file__-relative lookup of the
bundled SPA (<package>/static) breaks. Importing calliope.main as a package
module here keeps its __file__ at <_MEIPASS>/calliope/main.pyc, so
Path(__file__).parent / "static" resolves to the bundled calliope/static.

Dev flow is unaffected (python -m calliope.main still works); this file is only
used by calliope.spec.
"""

from __future__ import annotations

import sys

from calliope.main import main

if __name__ == "__main__":
    sys.exit(main())
