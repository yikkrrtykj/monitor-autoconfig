"""Liveness payload for the platform API service."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class HealthContext:
    clock: Callable[[], float]


def health_payload(context: HealthContext) -> dict:
    """Return the existing process-liveness response."""
    return {"ok": True, "time": int(context.clock())}
