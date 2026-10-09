"""Whether the prep work still describes the change under review.

Narration is only as good as its briefings: a hunk without a current
prep-review briefing is explained from the diff alone, and the reviewer
can't tell. So every connection and every diff refresh sends "prep_status"
with the counts and a ready-made message. Briefing itself is the
prep-review skill's, run in Claude Code or Cline, ideally in the session
that made the change (see docs/prep-skills.md)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from fastapi import WebSocket

from ..services.briefing_service import briefing_states, newest_investigated_briefing
from ..services.diff_service import Hunk
from ..web.config import CONFIG
from ..web.runtime import send_json
from ..web.session import Session
from .settings import context_status

# What to call each repo-level prep file when it predates HEAD. The settings
# panel has its own labels (settings.js, settings.ts).
_PREP_LABELS = {
    "project_overview": "project overview",
    "changeset": "change themes",
}
_SKILL_HINT = "Ask Claude Code or Cline: use the prep-review skill"


def prep_status(session: Session) -> dict:
    """The "prep_status" payload. Reads files and runs git: call off-thread."""
    repo_path = session.repo_path
    hunks = session.hunks
    # A pull request's "why" is its title, description and commits, not
    # per-hunk briefings nobody is expected to write, so none count as
    # missing there.
    states = ["current"] * len(hunks) if CONFIG["server"].get("base_sha") else briefing_states(repo_path, hunks)
    out_of_date = [i for i, state in enumerate(states) if state != "current"]
    changed = states.count("changed")
    context = context_status(repo_path)
    stale_context = [
        key for key, info in context.items() if key in _PREP_LABELS and info.get("present") and info.get("head_moved")
    ]
    latest_change = _latest_change(repo_path, hunks)
    latest_briefing = newest_investigated_briefing(repo_path)

    parts = []
    if out_of_date:
        parts.append(
            f"{len(out_of_date)} of {len(hunks)} changes have no up-to-date briefing"
            + (f" ({changed} changed after they were briefed)" if changed else "")
            + ", so Pear explains them from the diff alone and may guess at why they were made."
        )
        if latest_briefing is None:
            parts.append("Nothing has been briefed yet.")
        elif latest_change is not None:
            parts.append(f"Last code change {_when(latest_change)}; newest briefing {_when(latest_briefing)}.")
        # The extension's buttons copy this and open the assistant; the web
        # app shows only the text.
        parts.append(f"{_SKILL_HINT}.")
    if stale_context:
        names = " and ".join(_PREP_LABELS[key] for key in stale_context)
        hints = " ".join(f"{context[key]['refresh_hint']}." for key in stale_context)
        parts.append(f"The {names} predate the current commit. {hints}")

    return {
        "total": len(hunks),
        "out_of_date": out_of_date,
        "changed": changed,
        "stale_context": [_PREP_LABELS[key] for key in stale_context],
        "latest_change": _iso(latest_change),
        "latest_briefing": _iso(latest_briefing),
        "message": " ".join(parts) or None,
    }


async def send_prep_status(ws: WebSocket, session: Session) -> None:
    await send_json(ws, "prep_status", await asyncio.to_thread(prep_status, session))


def _latest_change(repo_path: str, hunks: list[Hunk]) -> float | None:
    """When a changed file was last written (mtime). Deleted files have none."""
    times = []
    for file_path in {hunk.file_path for hunk in hunks}:
        try:
            times.append((Path(repo_path) / file_path).stat().st_mtime)
        except OSError:
            continue
    return max(times, default=None)


def _iso(timestamp: float | None) -> str | None:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds") if timestamp else None


def _when(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp)
    return moment.strftime("%H:%M") if moment.date() == datetime.now().date() else moment.strftime("%d %b %H:%M")
