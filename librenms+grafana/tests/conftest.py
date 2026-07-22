"""Pytest collection config — make the tests/ directory's parent (the repo
component dir) importable, but more importantly the loader for the
hyphenated `generate-player-targets.py` module lives in the test file
itself, not here. conftest.py exists so pytest treats tests/ as a package.
"""
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
