"""Pull request review mode: the diff is taken against a base commit (the
merge-base the VS Code extension computes) instead of HEAD, and the session
is read-only — nothing that writes to the reviewed repo is allowed."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.handlers import HANDLERS, dispatch
from app.services.diff_service import get_review_hunks, line_history
from app.web.config import CONFIG
from app.web.session import Session


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def pr_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose HEAD is a "PR branch" two commits ahead of its base,
    plus an untracked file. Returns (repo, base sha)."""
    repo = tmp_path / "scratch"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.py").write_text("one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base commit")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "a.py").write_text("one = 1\ntwo = 22\nthree = 3\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "pr: change two")
    (repo / "b.py").write_text("new = True\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pr: add b")
    (repo / "scratch.txt").write_text("not part of the PR\n", encoding="utf-8")
    return repo, base


def test_base_sha_reviews_the_committed_pr_changes(pr_repo):
    repo, base = pr_repo

    hunks = get_review_hunks(str(repo), base)

    assert [h.file_path for h in hunks] == ["a.py", "b.py"]
    assert any(line == "+two = 22" for line in hunks[0].lines)
    assert all(h.base_ref == base for h in hunks)
    assert hunks[0].full_lines, "the full-file view is diffed against the base too"


def test_without_base_sha_committed_changes_are_not_a_review(pr_repo):
    repo, _ = pr_repo

    # Against HEAD the only change is the untracked file.
    assert [h.file_path for h in get_review_hunks(str(repo))] == ["scratch.txt"]


def test_line_history_reads_from_the_base_commit(pr_repo):
    repo, base = pr_repo
    hunk = get_review_hunks(str(repo), base)[0]

    history = line_history(str(repo), hunk)

    assert [entry.split(" ", 2)[2] for entry in history] == ["base commit"]


@pytest.fixture
def read_only(monkeypatch):
    monkeypatch.setitem(CONFIG["server"], "read_only", True)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


WRITERS = {"act_now", "refine_act_now", "confirm_act_now", "finish_review"}


def test_writers_are_exactly_the_handlers_that_change_the_repo():
    assert {name for name, spec in HANDLERS.items() if spec.writes} == WRITERS


@pytest.mark.parametrize("msg_type", sorted(WRITERS))
def test_read_only_review_refuses_writers(read_only, tmp_path, msg_type):
    ws = FakeSocket()
    session = Session([], None, str(tmp_path))
    session.pending_review_comments = [{"id": 1, "body": "x"}]

    asyncio.run(dispatch(ws, session, msg_type, {}))

    assert [m["type"] for m in ws.sent] == ["error"]
    assert ws.sent[0]["payload"]["source"] == msg_type
    assert "read-only" in ws.sent[0]["payload"]["message"]
    assert not (tmp_path / ".review").exists()
