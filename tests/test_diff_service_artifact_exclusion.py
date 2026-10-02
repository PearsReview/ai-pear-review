"""get_untracked_files/list_all_files exclude this app's own derived-cache
directories (.review/, .briefing/, .context/) from a target repo's
untracked-file listing.

Found live, not hypothetically: reviewing a real external repo twice
showed more "hunks" the second time — none of them the repo's own code,
all of them the app's own .briefing/*.json cache from the first run,
because that repo's .gitignore (reasonably) has no idea what .briefing/
is. The same gap let "All files" explore mode offer a .briefing/*.json
cache file as something to read for context. Neither directory is
special-cased in a target repo's .gitignore — they're this app's
artifacts, not the project being reviewed — so `git ls-files --others
--exclude-standard` lists them as ordinary untracked files once they
exist, and both functions used to hand them straight back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.diff_service import get_untracked_files, list_all_files


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo with one real tracked file, one real untracked file, and —
    critically — none of .review/.briefing/.context in its .gitignore,
    the same as any real project that has never heard of this app."""
    repo = tmp_path / "scratch"
    repo.mkdir()
    (repo / "real.py").write_text("def real():\n    return 1\n", encoding="utf-8")
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "t@e.st")
    _run_git(repo, "config", "user.name", "T")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "init")

    (repo / "new_untracked.py").write_text("def new():\n    return 2\n", encoding="utf-8")
    (repo / ".review").mkdir()
    (repo / ".review" / "session_state.json").write_text("{}", encoding="utf-8")
    (repo / ".briefing").mkdir()
    (repo / ".briefing" / "real.py__abcd1234.json").write_text("{}", encoding="utf-8")
    (repo / ".context").mkdir()
    (repo / ".context" / "call_map.json").write_text("{}", encoding="utf-8")
    return repo


def test_get_untracked_files_excludes_app_artifact_dirs(repo: Path):
    result = get_untracked_files(str(repo))
    assert "new_untracked.py" in result, "a real untracked file should still be listed"
    assert not any(_is_artifact(path) for path in result), (
        f"app artifact path leaked into get_untracked_files: {result}"
    )


def test_list_all_files_excludes_app_artifact_dirs(repo: Path):
    result = list_all_files(str(repo))
    assert "real.py" in result and "new_untracked.py" in result, "real files should still be listed"
    assert not any(_is_artifact(path) for path in result), f"app artifact path leaked into list_all_files: {result}"


def _is_artifact(path: str) -> bool:
    return path.replace("\\", "/").split("/", 1)[0] in (".review", ".briefing", ".context")
