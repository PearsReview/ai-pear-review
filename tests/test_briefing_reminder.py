"""The Stop-hook reminder (.claude/hooks/briefing_reminder.py): shown when
uncommitted hunks lack a current briefing, silent otherwise, and never in
the way."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "briefing_reminder.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A scratch repo carrying a copy of the prep-review scanner, since the
    hook loads it from the repo it's run in."""
    repo = tmp_path / "scratch"
    skill = repo / ".claude" / "skills" / "prep-review"
    skill.mkdir(parents=True)
    for name in ("scan_hunks.py", "write_briefing.py"):
        shutil.copy(ROOT / ".claude" / "skills" / "prep-review" / name, skill / name)
    (repo / ".gitignore").write_text(".briefing/\n.context/\n.claude/\n", encoding="utf-8")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run(repo: Path, session: str = "s1") -> str:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(HOOK)],
        input=json.dumps({"session_id": session}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**__import__("os").environ, "CLAUDE_PROJECT_DIR": str(repo)},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_clean_tree_is_silent(repo: Path):
    assert _run(repo) == ""


def test_unbriefed_change_reminds_once_per_count(repo: Path):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    message = json.loads(_run(repo))["systemMessage"]
    assert "1 of 1" in message and "/prep-review" in message
    assert _run(repo) == ""  # same session, same count: not repeated
    assert _run(repo, session="s2") != ""  # a different session is told


def test_briefed_change_is_silent(repo: Path):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("scan_hunks", repo / ".claude/skills/prep-review/scan_hunks.py")
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    hunk = scanner.scan(str(repo))[0]
    subprocess.run(
        [
            sys.executable,
            str(repo / ".claude/skills/prep-review/write_briefing.py"),
            "--repo",
            str(repo),
            "--file-path",
            hunk["file_path"],
            "--header",
            hunk["header"],
            "--content-hash",
            hunk["content_hash"],
            "--intent",
            "why",
        ],
        check=True,
        capture_output=True,
    )
    assert _run(repo) == ""


def test_not_a_repo_is_silent(tmp_path: Path):
    assert _run(tmp_path) == ""


# --- --auto-brief: block the stop so this session briefs what it edited ---


def _transcript(tmp_path: Path, *edited: Path, tool: str = "Edit") -> str:
    """A Claude Code transcript (JSONL) in which the session edited these files."""
    path = tmp_path / "transcript.jsonl"
    entries = [{"type": "user", "message": {"role": "user", "content": "make the change"}}]
    for file in edited:
        entries.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Editing."},
                        {"type": "tool_use", "id": "t", "name": tool, "input": {"file_path": str(file)}},
                    ],
                },
            }
        )
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n", encoding="utf-8")
    return str(path)


def _auto(repo: Path, transcript: str | None, session: str = "s1", **extra) -> dict | None:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(HOOK), "--auto-brief"],
        input=json.dumps({"session_id": session, "transcript_path": transcript, **extra}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**__import__("os").environ, "CLAUDE_PROJECT_DIR": str(repo)},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def test_auto_brief_blocks_once_for_the_sessions_unbriefed_hunks(repo: Path, tmp_path: Path):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    transcript = _transcript(tmp_path, repo / "a.py")
    out = _auto(repo, transcript)
    assert out["decision"] == "block"
    assert "a.py @@" in out["reason"] and "author-session" in out["reason"]
    assert _auto(repo, transcript) is None  # same unbriefed set: asked once, not on every stop


def test_auto_brief_never_blocks_while_already_continuing(repo: Path, tmp_path: Path):
    """stop_hook_active: Claude is already carrying on because of a Stop hook.
    Blocking again could loop."""
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert _auto(repo, _transcript(tmp_path, repo / "a.py"), stop_hook_active=True) is None


def test_auto_brief_leaves_changes_this_session_did_not_make(repo: Path, tmp_path: Path):
    """Someone else's uncommitted edits are not this session's to explain:
    asking would invite made-up reasons."""
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert _auto(repo, _transcript(tmp_path)) is None  # edited nothing
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    out = _auto(repo, _transcript(tmp_path, repo / "b.py", tool="Write"))
    assert "b.py" in out["reason"] and "a.py" not in out["reason"]


def test_auto_brief_without_a_transcript_is_silent(repo: Path):
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert _auto(repo, None) is None
    assert _auto(repo, str(repo / "no-such-transcript.jsonl")) is None


def test_auto_brief_lists_at_most_fifteen_hunks(repo: Path, tmp_path: Path):
    files = []
    for i in range(18):
        path = repo / f"new_{i:02}.py"
        path.write_text(f"n = {i}\n", encoding="utf-8")
        files.append(path)
    reason = _auto(repo, _transcript(tmp_path, *files))["reason"]
    listed = [line for line in reason.splitlines() if line.startswith("- ")]
    assert len(listed) == 15
    assert listed[0].startswith("- new_17.py")  # most recently edited first
    assert "3 more aren't listed" in reason


def test_the_apps_own_quick_briefing_does_not_count_as_briefed(repo: Path):
    """The app writes its diff-only guess ("generated") to the same file the
    skill would. Counting it would make every hunk the reviewer has looked
    at read as briefed."""
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("scan_hunks", repo / ".claude/skills/prep-review/scan_hunks.py")
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    hunk = scanner.scan(str(repo))[0]
    path = Path(hunk["briefing_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"content_hash": hunk["content_hash"], "intent": "a guess", "source": "generated"}),
        encoding="utf-8",
    )
    assert scanner.scan(str(repo))[0]["state"] == "missing"
    assert "1 of 1" in json.loads(_run(repo))["systemMessage"]
