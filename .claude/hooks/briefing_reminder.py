"""Claude Code Stop hook: remind, never block, when uncommitted hunks have
no current prep-review briefing.

The "why" behind a change is easiest to record in the session that made
it, and the review app's narration is only as good as its briefings (see
.claude/skills/prep-review/SKILL.md, author-session mode). This surfaces
the gap while that context still exists.

Reads the Stop hook payload on stdin ({"session_id": ...}) and prints
{"systemMessage": ...} or nothing. Shown only when the count of hunks
needing a briefing changes within a session, so a long session isn't
nagged every turn. Any failure (not a git repo, git missing, a timeout)
is silent with exit 0 — a reminder must never get in the way.

"Needs a briefing" is exactly what the skill's own scanner reports, by
importing scan_hunks.py rather than re-deriving it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_STATE_FILE = Path(".context") / ".briefing_reminder.json"
_MAX_SESSIONS_REMEMBERED = 20
_GIT_TIMEOUT_SECONDS = 5


def _load_scanner(repo: Path):
    path = repo / ".claude" / "skills" / "prep-review" / "scan_hunks.py"
    spec = importlib.util.spec_from_file_location("scan_hunks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_changes(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def reminder(repo: Path, session_id: str) -> str | None:
    """The message to show, or None. Records what was shown per session."""
    if not _has_changes(repo):
        return None
    hunks = _load_scanner(repo).scan(str(repo))
    todo = sum(1 for h in hunks if h["state"] != "fresh")

    state_path = repo / _STATE_FILE
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get(session_id) == todo:
        return None
    state.pop(session_id, None)
    state[session_id] = todo
    state = dict(list(state.items())[-_MAX_SESSIONS_REMEMBERED:])
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass

    if todo == 0:
        return None
    return (
        f"{todo} of {len(hunks)} uncommitted hunks have no current review briefing. "
        "Run /prep-review in author mode while the reason for the change is still fresh."
    )


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = str(payload.get("session_id") or "unknown")
        repo = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
        message = reminder(repo, session_id)
    except (Exception, SystemExit):  # scan_hunks raises SystemExit when git fails
        return
    if message:
        print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()
