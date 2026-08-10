"""Small, dependency-free filesystem primitives for platform state."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable


def ensure_directories(paths: Iterable[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def read_json_file(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    except json.JSONDecodeError:
        return fallback


def atomic_write_text(path: Path, text: str) -> None:
    """Preserve the entrypoint's temp-file then atomic-replace behavior."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)

def write_json_file(path: Path, payload, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if mode is None:
        temporary.write_text(text, encoding="utf-8")
    else:
        # Secret-bearing state is private from the instant it is created.
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
    temporary.replace(path)
