"""Claude Code Stop hook: get uncommitted hunks briefed while the session
that made them still knows why.

The "why" behind a change is easiest to record in the session that made
it, and the review app's narration is only as good as its briefings (see
.claude/skills/prep-review/SKILL.md, author-session mode). Two modes:

- Default (install_skills.py --with-reminders): remind, never block. Prints
  {"systemMessage": ...} when the count of hunks needing a briefing changes
  within a session, so a long session isn't nagged every turn.
- --auto-brief (install_skills.py --with-auto-brief): block the stop with
  {"decision": "block", "reason": ...}, which has Claude carry on in this
  same session and run prep-review. Only for hunks in files this session
  edited (its Edit/Write calls, from the transcript): another session's
  changes are not this one's to explain, and asking would invite made-up
  reasons. At most _MAX_HUNKS_PER_BLOCK hunks per block, once per set of
  unbriefed hunks, and never while Claude is already continuing because of
  this hook (stop_hook_active), so it can't loop.

Reads the Stop hook payload on stdin ({"session_id", "transcript_path",
"stop_hook_active"}). Any failure (not a git repo, git missing, a timeout,
an unreadable transcript) is silent with exit 0 — this must never get in
the way.

"Needs a briefing" is exactly what the skill's own scanner reports, by
importing scan_hunks.py rather than re-deriving it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_STATE_FILE = Path(".context") / ".briefing_reminder.json"
_MAX_STATE_ENTRIES = 40
_GIT_TIMEOUT_SECONDS = 5
# About 14 s a hunk: enough to cover a normal task without holding a turn
# open for many minutes. The rest are named in the reason, not briefed.
_MAX_HUNKS_PER_BLOCK = 15
# Tools whose input names a file this session wrote to.
_EDIT_TOOLS = {"Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}


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


def _read_state(repo: Path) -> dict:
    try:
        state = json.loads((repo / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def _remember(repo: Path, key: str, value: object) -> None:
    state = _read_state(repo)
    state.pop(key, None)
    state[key] = value
    state = dict(list(state.items())[-_MAX_STATE_ENTRIES:])
    path = repo / _STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def reminder(repo: Path, session_id: str) -> str | None:
    """The message to show, or None. Records what was shown per session."""
    if not _has_changes(repo):
        return None
    hunks = _load_scanner(repo).scan(str(repo))
    todo = sum(1 for h in hunks if h["state"] != "fresh")
    if _read_state(repo).get(session_id) == todo:
        return None
    _remember(repo, session_id, todo)
    if todo == 0:
        return None
    return (
        f"{todo} of {len(hunks)} uncommitted hunks have no current review briefing. "
        "Run /prep-review in author mode while the reason for the change is still fresh."
    )


def _path_key(path: str) -> str:
    return os.path.normcase(path.replace("\\", "/"))


def edited_files(transcript_path: str, repo: Path) -> list[str]:
    """Files this session wrote to, as repo-relative path keys, most
    recently edited first. Files outside the repo are dropped."""
    root = os.path.abspath(repo)
    order: dict[str, int] = {}
    with open(transcript_path, encoding="utf-8") as transcript:
        for line in transcript:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                field = _EDIT_TOOLS.get(block.get("name"))
                target = (block.get("input") or {}).get(field) if field else None
                if not isinstance(target, str) or not target:
                    continue
                absolute = os.path.abspath(target if os.path.isabs(target) else os.path.join(root, target))
                try:
                    relative = os.path.relpath(absolute, root)
                except ValueError:  # another drive
                    continue
                if relative.startswith(".."):
                    continue
                key = _path_key(relative)
                order.pop(key, None)
                order[key] = len(order)
    return list(reversed(order))


def auto_brief(repo: Path, session_id: str, transcript_path: str | None) -> str | None:
    """The reason to block the stop with, or None to let it stop."""
    if not transcript_path or not _has_changes(repo):
        return None
    files = edited_files(transcript_path, repo)
    if not files:
        return None
    rank = {key: i for i, key in enumerate(files)}
    todo = [
        h for h in _load_scanner(repo).scan(str(repo)) if h["state"] != "fresh" and _path_key(h["file_path"]) in rank
    ]
    if not todo:
        return None
    # Once per set of unbriefed hunks: if Claude couldn't or wouldn't brief
    # them last time, asking again on every stop would only nag.
    signature = hashlib.sha256("\n".join(sorted(h["content_hash"] for h in todo)).encode()).hexdigest()[:16]
    key = f"{session_id}:auto-brief"
    if _read_state(repo).get(key) == signature:
        return None
    _remember(repo, key, signature)

    todo.sort(key=lambda h: (rank[_path_key(h["file_path"])], h["index"]))
    listed, rest = todo[:_MAX_HUNKS_PER_BLOCK], todo[_MAX_HUNKS_PER_BLOCK:]
    lines = [
        f"{len(todo)} uncommitted change(s) in files you edited this session have no review briefing. "
        "Before you stop, run the prep-review skill (.claude/skills/prep-review/SKILL.md) in author-session "
        "mode for these hunks only, recording why you made each change:",
        *(f"- {h['file_path']} {h['header']}" for h in listed),
        "If a listed hunk isn't your edit (someone else's change in the same file), skip it rather than guess "
        "why it was made. Changes you made through shell commands or subagents aren't listed; brief those too "
        "if you made them.",
    ]
    if rest:
        lines.append(
            f"{len(rest)} more aren't listed. Don't brief them now; say in your closing message that they still "
            "need /prep-review."
        )
    lines.append("If you can't brief them, say so in a sentence and stop.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-brief", action="store_true")
    args, _ = parser.parse_known_args()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        session_id = str(payload.get("session_id") or "unknown")
        repo = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd())
        if args.auto_brief:
            if payload.get("stop_hook_active"):
                return
            reason = auto_brief(repo, session_id, payload.get("transcript_path"))
            if reason:
                print(json.dumps({"decision": "block", "reason": reason}))
            return
        message = reminder(repo, session_id)
    except (Exception, SystemExit):  # scan_hunks raises SystemExit when git fails
        return
    if message:
        print(json.dumps({"systemMessage": message}))


if __name__ == "__main__":
    main()
