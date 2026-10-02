"""get_review_hunks copes with files that aren't plain ASCII/UTF-8 — the
shape of most long-lived codebases, not an edge case.

Both were found by pointing the app at scratch repos, not hypothetically:

- A changed file saved as Latin-1 made git's output undecodable as UTF-8.
  The strict decode failed inside subprocess's reader thread, stdout came
  back as None, and the WebSocket connect path died on an AttributeError
  rather than a DiffError.
- A changed file named "café.py" came back as file_path "unknown" with an
  empty full-file view, because git C-quotes non-ASCII paths by default and
  the quoted "diff --git" line matched nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.code_search import find_definition
from app.services.diff_service import (
    _file_path_from_block,
    _unquote_git_path,
    get_review_hunks,
    get_untracked_files,
)


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "t@e.st")
    _run_git(repo, "config", "user.name", "T")
    _run_git(repo, "config", "core.autocrlf", "false")
    return repo


def _commit_all(repo: Path) -> None:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")


def test_latin1_file_is_reviewable(repo: Path):
    (repo / "legacy.py").write_bytes(b"caf\xe9 = 1\n")
    _commit_all(repo)
    (repo / "legacy.py").write_bytes(b"caf\xe9 = 2\n")

    hunks = get_review_hunks(str(repo))

    assert [h.file_path for h in hunks] == ["legacy.py"]
    assert hunks[0].full_lines, "the full-file view should still render"
    assert any("= 2" in line for line in hunks[0].lines)


def test_untracked_latin1_file_is_reviewable(repo: Path):
    (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo)
    (repo / "new_legacy.py").write_bytes(b"na\xefve = True\n")

    hunks = get_review_hunks(str(repo))

    assert [h.file_path for h in hunks] == ["new_legacy.py"]
    assert hunks[0].full_lines


@pytest.mark.parametrize("name", ["café.py", "my file.py", "dir b/inner.py"])
def test_unusual_file_names_keep_their_path(repo: Path, name: str):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("a = 1\n", encoding="utf-8")
    _commit_all(repo)
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")

    hunks = get_review_hunks(str(repo))

    assert [h.file_path for h in hunks] == [name]
    assert hunks[0].full_lines, "the full-file view should render, not come back empty"
    assert hunks[0].highlight_start >= 0


def test_untracked_non_ascii_name_is_listed_unquoted(repo: Path):
    (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
    _commit_all(repo)
    (repo / "naïve.py").write_text("y = 2\n", encoding="utf-8")

    assert get_untracked_files(str(repo)) == ["naïve.py"]
    assert [h.file_path for h in get_review_hunks(str(repo))] == ["naïve.py"]


def test_added_line_starting_with_plus_plus_is_not_read_as_a_path():
    block = [
        "diff --git a/notes.md b/notes.md",
        "index 1111111..2222222 100644",
        "--- a/notes.md",
        "+++ b/notes.md",
        "@@ -1 +1,2 @@",
        " intro",
        "+++ not a header",
    ]
    assert _file_path_from_block(block) == "notes.md"


def test_deleted_file_uses_the_old_path():
    block = [
        "diff --git a/gone.py b/gone.py",
        "deleted file mode 100644",
        "--- a/gone.py",
        "+++ /dev/null",
        "@@ -1 +0,0 @@",
        "-x = 1",
    ]
    assert _file_path_from_block(block) == "gone.py"


@pytest.mark.parametrize(
    ("quoted", "expected"),
    [
        ('"caf\\303\\251.py"', "café.py"),
        ('"tab\\there.py"', "tab\there.py"),
        ('"say \\"hi\\".py"', 'say "hi".py'),
        ("plain.py", "plain.py"),
    ],
)
def test_unquote_git_path(quoted: str, expected: str):
    assert _unquote_git_path(quoted) == expected


def test_step_into_survives_a_latin1_match(repo: Path):
    (repo / "legacy.py").write_bytes(b"# caf\xe9\ndef target():\n    return 1\n")
    _commit_all(repo)

    definition = find_definition(str(repo), "target")

    assert definition.file_path == "legacy.py"
    assert definition.line_number == 2
