"""Pytest collection config — make the tests/ directory's parent (the repo
component dir) importable, but more importantly the loader for the
hyphenated `generate-player-targets.py` module lives in the test file
itself, not here. conftest.py exists so pytest treats tests/ as a package.
"""
