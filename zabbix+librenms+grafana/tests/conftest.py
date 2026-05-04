"""Test fixtures for monitor-autoconfig parsing logic.

The script ships with a hyphenated filename (generate-player-targets.py)
because its container path is fixed by docker-compose, so we load it via
importlib instead of import statement.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Loaded once per test session.
gpt = _load_module("generate_player_targets", "generate-player-targets.py")
