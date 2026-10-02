"""WebSocket message handlers, one module per area. Importing this package
registers every handler in registry.HANDLERS, which is why server.py
imports dispatch from here rather than from registry directly."""

from . import (  # noqa: F401 — registers handlers
    act_now,
    comments,
    explore,
    narration,
    recording,
    research,
    review_flow,
    settings,
    voice,
)
from .registry import HANDLERS, dispatch

__all__ = ["HANDLERS", "dispatch"]
