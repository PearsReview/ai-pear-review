"""Reads the change-set themes the prep-review skill left behind: the
overall reason a group of hunks exists, written once and referenced by id
from each hunk's briefing (see .claude/skills/prep-review/write_theme.py).

Why a separate file rather than a field on every briefing: one refactor
can touch 90 hunks, and 90 copies of "why we did this" drift apart and
spend 90 hunks' worth of prompt budget saying the same thing.

Stored under .context/ for the reason project_overview.py gives: a theme,
especially one recorded by the session that authored the change, is
expensive or impossible to recreate, and per-hunk briefings under
.briefing/ are pruned as the diff moves. Staleness is reported, never
grounds for discarding, same as the overview — HEAD moving doesn't make
"this was a refactor for X" false.

Never calls a model and never writes; the skill owns the file.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CONTEXT_DIR = ".context"
_CHANGESET_FILENAME = "changeset.json"

# Match write_theme.py's caps. The skill refuses longer text; these are the
# safety net for a hand-edited file, not the contract.
MAX_TITLE_CHARS = 80
MAX_WHY_CHARS = 400


def changeset_path(repo_path: str) -> Path:
    return Path(repo_path) / _CONTEXT_DIR / _CHANGESET_FILENAME


def load_changeset(repo_path: str) -> dict | None:
    """The stored change set, or None if there isn't a usable one. Absence is
    the normal case — the skill is optional."""
    path = changeset_path(repo_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.info("Ignoring unreadable change set at %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("themes"), list):
        return None
    return data


def theme(repo_path: str, theme_id: str | None) -> dict | None:
    """{"id", "title", "why", "source"} for one theme, text capped, or None
    when the id is missing, unknown, or has no `why` to offer."""
    if not theme_id:
        return None
    changeset = load_changeset(repo_path)
    if changeset is None:
        return None
    for entry in changeset["themes"]:
        if isinstance(entry, dict) and entry.get("id") == theme_id and entry.get("why"):
            return {
                "id": theme_id,
                "title": str(entry.get("title") or theme_id)[:MAX_TITLE_CHARS].strip(),
                "why": str(entry["why"])[:MAX_WHY_CHARS].strip(),
                "source": entry.get("source"),
            }
    return None


def changeset_status(repo_path: str, head_sha: str | None) -> dict:
    """For the settings panel's prep-file list — reporting only."""
    changeset = load_changeset(repo_path)
    if changeset is None:
        return {"present": False}
    stored_sha = changeset.get("base_sha")
    return {
        "present": True,
        "generated_at": changeset.get("generated_at"),
        "theme_count": len(changeset["themes"]),
        "head_sha": stored_sha,
        "current_head_sha": head_sha,
        "head_moved": bool(stored_sha and head_sha and stored_sha != head_sha),
    }
