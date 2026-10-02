"""Lists the hunks the app will review, with the exact cache key and hash
each briefing must carry.

Run from the repo being reviewed:

    python .claude/skills/prep-review/scan_hunks.py [--repo PATH] [--todo-only]

Why this exists rather than having the model work the keys out: the app
silently ignores a briefing whose content_hash doesn't match the hunk's
diff byte-for-byte (see _load_cached in app/services/briefing_service.py).
A near-miss doesn't warn, it just never gets used — so the keys are
derived here by the same rules the app itself applies, not retyped by
hand.

Deliberately standalone: no imports from app.*, so the skill keeps working
against a target repo that isn't this project (the app reads .briefing/
from whatever repo_path points at). That means the diff-splitting rules
below are a faithful COPY of app/services/diff_service.py's, and drift
between the two would show up as briefings that are written but never
read. tests/test_prep_review_keys.py pins them together.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")
_GIT_TIMEOUT_SECONDS = 15


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _split_file_blocks(diff_text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _is_binary_block(block: list[str]) -> bool:
    return any(line.startswith("Binary files ") and line.endswith("differ") for line in block)


def _is_rename_only_block(block: list[str]) -> bool:
    has_rename_marker = any(line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")) for line in block)
    return has_rename_marker and not any(HUNK_HEADER_RE.match(line) for line in block)


def _file_path_from_block(block: list[str]) -> str:
    match = _DIFF_GIT_RE.match(block[0])
    if match:
        return match.group(2) or match.group(1)  # prefer the post-change path
    return "unknown"


def _hunks_from_block(block: list[str], file_path: str) -> list[dict]:
    hunks: list[dict] = []
    header = ""
    lines: list[str] | None = None
    for line in block:
        if HUNK_HEADER_RE.match(line):
            if lines is not None:
                hunks.append({"file_path": file_path, "header": header, "lines": lines})
            header = line
            lines = [line]
        elif lines is not None:
            lines.append(line)
    if lines is not None:
        hunks.append({"file_path": file_path, "header": header, "lines": lines})
    return hunks


def _untracked_hunks(repo: str) -> list[dict]:
    """Untracked files are reviewed as one whole-file "all added" hunk each,
    appended after every tracked hunk — mirroring get_review_hunks. Empty
    files produce nothing, same as the app."""
    hunks: list[dict] = []
    for rel in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        rel = rel.strip()
        if not rel:
            continue
        try:
            text = (Path(repo) / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable — the app skips these too
        file_lines = text.splitlines()
        if not file_lines:
            continue
        header = f"@@ -0,0 +1,{len(file_lines)} @@"
        hunks.append({"file_path": rel, "header": header, "lines": [header] + [f"+{line}" for line in file_lines]})
    return hunks


def briefing_path(repo: str, file_path: str, header: str) -> Path:
    """Same key the app derives: file path with separators flattened, plus
    the first 8 hex of sha256(header) — stable across hunk-index drift."""
    safe_file = file_path.replace("/", "__").replace("\\", "__")
    header_hash = hashlib.sha256(header.encode("utf-8")).hexdigest()[:8]
    return Path(repo) / ".briefing" / f"{safe_file}__{header_hash}.json"


def content_hash(diff_context: str) -> str:
    return hashlib.sha256(diff_context.encode("utf-8")).hexdigest()


def scan(repo: str) -> list[dict]:
    hunks: list[dict] = []
    for block in _split_file_blocks(_git(repo, "diff", "HEAD", "--no-color", "--unified=3")):
        if _is_binary_block(block) or _is_rename_only_block(block):
            continue
        hunks.extend(_hunks_from_block(block, _file_path_from_block(block)))
    hunks.extend(_untracked_hunks(repo))

    out: list[dict] = []
    for index, hunk in enumerate(hunks):
        diff_context = "\n".join(hunk["lines"])
        path = briefing_path(repo, hunk["file_path"], hunk["header"])
        existing = _existing_state(path, diff_context)
        out.append(
            {
                "index": index,
                "file_path": hunk["file_path"],
                "header": hunk["header"],
                "diff": diff_context,
                "content_hash": content_hash(diff_context),
                "briefing_path": str(path),
                "state": existing,
            }
        )
    return out


def _existing_state(path: Path, diff_context: str) -> str:
    """fresh   — a usable briefing already matches this exact diff
    stale   — one exists but the code changed under it (app ignores it)
    missing — nothing written yet
    Mirrors _load_cached's own checks, including its "empty intent means
    an unfilled skeleton, treat as unusable" rule."""
    if not path.exists():
        return "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "stale"
    if data.get("content_hash") != content_hash(diff_context):
        return "stale"
    return "fresh" if data.get("intent") else "missing"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repo to scan (default: cwd)")
    parser.add_argument("--todo-only", action="store_true", help="only hunks needing work (missing or stale)")
    args = parser.parse_args()

    hunks = scan(args.repo)
    if args.todo_only:
        hunks = [h for h in hunks if h["state"] != "fresh"]
    json.dump(hunks, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
