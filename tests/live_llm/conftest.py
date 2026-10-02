"""In-process rapid accuracy testing against a real target repo's real
diff — no Playwright, no browser, no FastAPI server subprocess, no
WebSocket protocol. Calls app.services.conversation_service directly, the
same functions app/server.py's WS handlers call, just without the
transport wrapped around them.

Deliberately living under tests/, not qa_agent/: tests/ already imports
app.* directly, per its own convention (see the top-level README's
Testing section — tests/ is plain pytest that imports app.*; qa_agent/ is
external/black-box only, driving a real subprocess + browser on purpose).
This suite is squarely internal, so it belongs here, not there.

What this buys over qa_agent/live/: no browser launch, no WS round-trip,
no "click Next N times to reach hunk K" walk — any hunk/file/question is
one direct function call away, so a question about hunk 7 costs exactly
one LLM call, not seven. What it does NOT buy: the LLM call itself is
still the same real Ollama round-trip (typically 15-40s), which dominates
either way — this removes transport and walk overhead, not model latency.

What it deliberately does NOT cover, unlike qa_agent/live/: the actual
WebSocket protocol, the browser UI, or anything server.py's handlers do
beyond building a prompt and calling the LLM (session state, persistence,
error frames, WS message shapes) — qa_agent/live/ still owns that
coverage, and this suite is not a replacement for it.

Writes result packs into qa_agent/live/results/ (not a directory of its
own) so the existing judge-live-review skill — built for qa_agent/live/'s
packs — can judge these too with no changes: same
*_live_review_pack.json naming convention scan_pack.py already globs for.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = REPO_ROOT / "qa_agent" / "live" / "results"


def pytest_addoption(parser: pytest.Parser) -> None:
    try:
        parser.addoption(
            "--target-repo",
            action="store",
            default=None,
            help="Path to the git repo whose real uncommitted changes to ask questions about. Required.",
        )
    except ValueError:
        # Already registered by qa_agent/live/conftest.py's own addoption —
        # only possible if both suites are collected in the same session
        # (neither is meant to be run that way, but a bare `pytest .` at
        # the repo root would hit this without the guard). Whichever
        # conftest's addoption runs first wins; both define the same
        # default and semantics, so it doesn't matter which.
        pass


def _remove_app_artifacts(path: Path) -> None:
    """Identical to qa_agent/live/conftest.py's own helper of the same
    name — see that copy's docstring for the full reasoning, including why
    `.context/` is never wiped wholesale (it can hold a hand-authored
    project_overview.json meant to persist across runs). Duplicated, not
    imported, following this project's "small local duplication over a
    cross-suite import" convention for the tests/ <-> qa_agent boundary.

    Needed here for the same reason it's needed there: briefing_client's
    analyze_hunk() writes real .briefing/*.json cache files straight into
    target_repo (no server subprocess or copy in between — this suite
    calls it in-process), so without this cleanup a run leaves the exact
    same untracked-file cruft qa_agent/live/ once did."""
    for name in (".review", ".briefing"):
        shutil.rmtree(path / name, ignore_errors=True)
    for name in ("call_map.json", "call_map.md"):
        (path / ".context" / name).unlink(missing_ok=True)


@pytest.fixture(scope="session")
def target_repo(pytestconfig: pytest.Config, request: pytest.FixtureRequest) -> Path:
    raw = pytestconfig.getoption("--target-repo")
    if not raw:
        raise pytest.UsageError("pass --target-repo PATH: a git repo with uncommitted changes to ask questions about")
    path = Path(raw).resolve()
    if not (path / ".git").exists():
        raise pytest.UsageError(f"--target-repo {path} is not a git repository (no .git found)")
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=path, capture_output=True, text=True)
    if not diff.stdout.strip():
        raise pytest.UsageError(
            f"--target-repo {path} has no uncommitted changes (git diff vs HEAD is empty) — "
            "there is nothing to ask questions about. Make some changes there first."
        )
    _remove_app_artifacts(path)
    request.addfinalizer(lambda: _remove_app_artifacts(path))
    return path


@pytest.fixture(scope="session")
def hunks(target_repo: Path) -> list:
    from app.services.diff_service import get_review_hunks

    result = get_review_hunks(str(target_repo))
    if not result:
        pytest.skip(f"{target_repo} has no reviewable hunks")
    return result


# Overrides app/config.yaml's conversation.timeout_seconds (60) for THIS
# harness's own ConversationClient only — never written back to the real
# config. Confirmed live, twice: this suite's own first real call hit that
# 60s ceiling (once as this app's own wall-clock ConversationError, once
# as requests' lower-level socket read timeout) on a cold model load —
# `ollama ps` at the time showed size_vram: 0, i.e. genuinely CPU-only
# inference, consistent with this project's own documented ~40s/hunk
# figure being an OPTIMISTIC case, not a worst one. A real reviewer whose
# first request lands right after Ollama's keep_alive unloads the model
# could hit the exact same production ceiling — this is a real app-level
# finding (worth deciding on separately, e.g. warming the model at
# startup, or raising the default), not something to paper over by
# quietly retrying in this harness alone.
_TEST_TIMEOUT_SECONDS = 180


@pytest.fixture(scope="session")
def conversation():
    """A real ConversationClient built from this project's own
    app/config.yaml — same object app/server.py builds per WebSocket
    connection, just constructed directly instead of by a WS handshake,
    with timeout_seconds raised for this harness only (see
    _TEST_TIMEOUT_SECONDS above). Session-scoped: cheap to construct (no
    network call at construction time for the ollama provider — see
    ConversationClient's own docstring), and every test sharing one client
    just means shared usage counters, which nothing here reads."""
    from app.services.conversation_service import ConversationClient
    from app.web.config import CONFIG

    config = {**CONFIG["conversation"], "timeout_seconds": _TEST_TIMEOUT_SECONDS}
    return ConversationClient(config, CONFIG.get("debug"))


@pytest.fixture(scope="session")
def briefing_client(target_repo: Path):
    from app.services.briefing_service import BriefingClient
    from app.web.config import CONFIG

    return BriefingClient(CONFIG["briefing"], str(target_repo))


def project_context_for(target_repo: Path, all_hunks: list, hunk) -> str | None:
    """Reimplements app/web/context.py's build_project_context without needing
    a real Session — that function only ever reads session.repo_path and
    session.hunks, so a plain SimpleNamespace duck-types it exactly.
    Kept as a call into the app's own function (not a reimplementation of
    its logic) so this suite can never silently drift from what a real
    WS-connected session actually sends."""
    from app.web.context import build_project_context

    fake_session = types.SimpleNamespace(repo_path=str(target_repo), hunks=all_hunks)
    return build_project_context(fake_session, hunk)


@pytest.fixture(scope="session")
def call_map_data(target_repo: Path) -> dict | None:
    """Identical to qa_agent/live/conftest.py's own fixture of the same
    name — see that copy's docstring for the full reasoning. No ordering
    dependency on `hunks` needed here (unlike that copy's dependency on
    total_hunks): app/services/diff_service.py's get_untracked_files/
    list_all_files now exclude .review/.briefing/.context at the source
    (see tests/test_diff_service_artifact_exclusion.py), so this fixture's
    own writes can no longer corrupt hunk counting regardless of when it
    runs — the defensive ordering trick was a workaround for a bug that no
    longer exists."""
    script = REPO_ROOT / ".claude" / "skills" / "call-map" / "scan_calls.py"
    result = subprocess.run(
        [sys.executable, str(script), "--repo", str(target_repo)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    path = target_repo / ".context" / "call_map.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


@pytest.fixture(scope="session")
def judge_model_config() -> dict:
    """Identical to qa_agent/live/conftest.py's own fixture of the same
    name — see that copy's docstring for the full reasoning. Duplicated,
    not imported, same convention as target_repo above."""
    import re

    config_text = (REPO_ROOT / "app" / "config.yaml").read_text(encoding="utf-8")
    block_match = re.search(r"^\s*ollama:\s*\n((?:^[ \t]{4,}.*\n?)+)", config_text, re.MULTILINE)
    if not block_match:
        raise AssertionError("app/config.yaml's conversation.ollama: block was not found")
    block = block_match.group(1)
    base_url_match = re.search(r"base_url:\s*(\S+)", block)
    model_match = re.search(r"model:\s*(\S+)", block)
    config = {"base_url": base_url_match.group(1), "model": model_match.group(1)}
    num_ctx_match = re.search(r"num_ctx:\s*(\d+)", block)
    if num_ctx_match:
        config["num_ctx"] = int(num_ctx_match.group(1))
    return config
