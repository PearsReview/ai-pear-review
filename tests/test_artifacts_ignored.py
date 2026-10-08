"""session_store.ensure_artifacts_ignored — keeping the app's own working
files out of the reviewed repo's `git status`.

The app writes .review/, .briefing/ and .context/ into whatever repo it is
pointed at. That repo's .gitignore has no reason to know about a tool its
author may never have run, so a first review used to leave three untracked
directories behind — one of them (.review/) holding hand-off documents that
quote the code, and the saved voice-service token.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.services.session_store import ensure_artifacts_ignored


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def _exclude_text(repo: Path) -> str:
    return (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def test_adds_every_artifact_directory(repo: Path):
    added = ensure_artifacts_ignored(str(repo))

    assert added == [
        "/.review/",
        "/.briefing/",
        "/.briefing_debug/",
        "/.editor_debug/",
        "/.context/",
        "/.claude/skills/apply-review/",
    ]
    text = _exclude_text(repo)
    for pattern in added:
        assert pattern in text


def test_git_actually_ignores_them_afterwards(repo: Path):
    """The point of the whole function — asserted against real git rather
    than against the file's contents."""
    ensure_artifacts_ignored(str(repo))
    (repo / ".review").mkdir()
    (repo / ".review" / "session_state.json").write_text("{}", encoding="utf-8")
    skill = repo / ".claude" / "skills" / "apply-review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("plan", encoding="utf-8")

    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)

    assert status.stdout.strip() == ""


def test_existing_content_is_kept(repo: Path):
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text("# my own notes\nscratch.txt\n", encoding="utf-8")

    ensure_artifacts_ignored(str(repo))

    text = _exclude_text(repo)
    assert "scratch.txt" in text
    assert "/.review/" in text


def test_a_second_run_changes_nothing(repo: Path):
    ensure_artifacts_ignored(str(repo))
    before = _exclude_text(repo)

    assert ensure_artifacts_ignored(str(repo)) == []
    assert _exclude_text(repo) == before


def test_a_reviewer_s_own_spelling_counts_as_handled(repo: Path):
    """They may have added these to .gitignore or exclude themselves, in any
    of the several forms git accepts. Adding ours on top would be noise."""
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(".review/\n.briefing\n.context/**\n/.briefing_debug\n.editor_debug/\n", encoding="utf-8")

    # Only what they haven't covered is added.
    assert ensure_artifacts_ignored(str(repo)) == ["/.claude/skills/apply-review/"]


def test_a_longer_name_doesnt_count_as_a_shorter_one(repo: Path):
    """.briefing_debug/ in the exclude file doesn't mean .briefing/ is."""
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(".briefing_debug/\n", encoding="utf-8")
    assert "/.briefing/" in ensure_artifacts_ignored(str(repo))


def test_a_linked_worktree_uses_the_main_repository_s_exclude(repo: Path, tmp_path: Path):
    """A pull request checked out with "Checkout in Worktree" has .git as a
    file. git reads info/exclude from the main repository for every
    worktree, so that's where the patterns go — and the worktree's
    `git status` is clean of this app's directories."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run([*git, "add", "a.txt"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=repo, check=True)
    worktree = tmp_path / "pr-worktree"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(worktree)], cwd=repo, check=True)
    assert (worktree / ".git").is_file()

    assert "/.review/" in ensure_artifacts_ignored(str(worktree))
    (worktree / ".review").mkdir()
    (worktree / ".review" / "x.json").write_text("{}", encoding="utf-8")
    (worktree / ".briefing_debug").mkdir()
    (worktree / ".briefing_debug" / "p.txt").write_text("p", encoding="utf-8")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True, check=True
    ).stdout
    assert status == ""


def test_a_directory_that_is_not_a_git_repo_is_left_alone(tmp_path: Path):
    assert ensure_artifacts_ignored(str(tmp_path)) == []
    assert not (tmp_path / ".git").exists()


def test_a_git_file_worktree_is_left_alone(tmp_path: Path):
    """A linked worktree or submodule has .git as a file. Writing
    .git/info/exclude under it would create a bogus directory."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")

    assert ensure_artifacts_ignored(str(tmp_path)) == []
    assert (tmp_path / ".git").is_file()
