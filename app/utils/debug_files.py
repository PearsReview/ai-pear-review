"""Writes best-effort debug files to a local, gitignored directory.

Two callers, both in app/services/. briefing_service.py dumps every prompt
it sends to the conversation connection here, as an audit trail.
editor_service.py's build_change_request uses it as a persistent record of
every change request generated, and as a fallback if the browser's
clipboard write silently fails.
"""

from __future__ import annotations

from pathlib import Path


def write_debug_prompt(debug_dir: str, filename: str, prompt: str) -> None:
    """Best-effort: a write failure here shouldn't break whatever it's
    supporting, so any OSError (missing permissions, a full disk, ...) is
    swallowed rather than raised."""
    try:
        path = Path(debug_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / filename).write_text(prompt, encoding="utf-8")
    except OSError:
        pass
