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
