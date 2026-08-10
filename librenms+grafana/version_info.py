"""Platform software and configuration schema version helpers."""
from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path


CURRENT_SCHEMA_VERSION = 1
MODULE_DIR = Path(__file__).resolve().parent


def _version_candidates() -> tuple[Path, ...]:
    configured = os.environ.get("PLATFORM_VERSION_FILE", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend((MODULE_DIR / "VERSION", MODULE_DIR.parent / "VERSION"))
    return tuple(dict.fromkeys(candidates))


@lru_cache(maxsize=1)
def get_platform_version() -> str:
    for path in _version_candidates():
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return "unknown"


@lru_cache(maxsize=1)
def get_git_commit() -> str:
    configured = os.environ.get("PLATFORM_GIT_COMMIT", "").strip()
    if configured:
        return configured

    repository = MODULE_DIR.parent
    if not (repository / ".git").exists():
        return "unknown"
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def get_version_info() -> dict[str, str | int]:
    return {
        "platform_version": get_platform_version(),
        "git_commit": get_git_commit(),
        "config_schema_supported": CURRENT_SCHEMA_VERSION,
    }


if __name__ == "__main__":
    info = get_version_info()
    print(info["platform_version"])
    print(info["git_commit"])
    print(info["config_schema_supported"])
