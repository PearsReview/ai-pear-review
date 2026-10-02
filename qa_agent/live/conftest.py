"""Fixtures for driving the app against a real, external repo's actual
uncommitted changes — as opposed to the sibling suites next door, which are
built specifically to *avoid* that (qa_agent/conftest.py's fixed scratch
repo, qa_agent/generated/'s seeded one). Both exist so a run is
deterministic regardless of what's uncommitted in a real working tree; this
suite exists for the opposite reason — to prove the app actually works on
someone's real, messy diff, not just on repos built to be easy.

Consequences of that:

- No RepoSpec oracle. There is no "changed_symbols" ground truth for an
  arbitrary repo, so this suite can only make deterministic assertions
  about the *app* (did every hunk narrate, did nothing error) — not about
  whether a narration is a *correct* description of unknown code. That
  judgment is left to a human/Claude Code session reading the pack this
  suite writes (see test_live_review.py).
- --target-repo, not a fixture-generated path. Required; any git repo
  with uncommitted changes works.
- A free port, like qa_agent/generated/ — so this can run alongside the
  fixed suite (or a developer's own `python run.py`) without colliding on
  8765.
- Still copies app/+static/+run.py to a tmp dir and patches the copy's
  config.yaml, never the real project's — same reasoning as both sibling
  suites' app_dir/app_copy_dir fixtures: a hard-killed test process can't
  run a save/restore, so patch-in-place has a real failure mode a copy
  doesn't.

Excluded from `pytest qa_agent/` (see qa_agent/conftest.py's
collect_ignore) for the same reason qa_agent/generated/ is: it needs its
own explicit invocation (`pytest qa_agent/live/`), takes real minutes
against a real local model, and depends on an external repo path existing
on this machine.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def pytest_addoption(parser: pytest.Parser) -> None:
    try:
        parser.addoption(
            "--target-repo",
            action="store",
            default=None,
            help="Path to the git repo whose real uncommitted changes the app should review. Required.",
        )
    except ValueError:
        # Already registered by tests/live_llm/conftest.py's own
        # addoption — only reachable if both suites are collected in the
        # same session (neither is meant to be run that way). See that
        # copy's identical guard for the full reasoning.
        pass


def _remove_app_artifacts(path: Path) -> None:
    """Removes the app's own `.review/` and `.briefing/` dirs from
    target_repo, plus `.context/call_map.json`/`.md` specifically (never
    the whole `.context/` directory — see below).

    Confirmed live: a target repo's own .gitignore has no reason to know
    about any of these — they're this app's (or this suite's) artifacts,
    not the target project's — so `git ls-files --others --exclude-
    standard` (what both diff_service.get_untracked_files and this suite's
    own total_hunks fixture use) lists them as real untracked files once
    written. The app then treats every one of them as an "all added" hunk
    on the *next* diff read, same as any other untracked file — so a repo
    reviewed twice without this cleanup shows more and more hunks each
    time, none of them the target project's own code. Worth knowing
    outside this suite too: anyone running `python run.py` by hand against
    a repo whose .gitignore doesn't exclude .review/ and .briefing/ will
    see the identical thing on a second run.

    `.context/` is NOT wiped wholesale like the other two, deliberately:
    unlike .review/.briefing (purely transient — session state, per-hunk
    cache), .context/ can also hold a hand-authored
    project_overview.json/.md from the project-overview skill — durable,
    meant to persist and be reused across many runs (see that skill's own
    SKILL.md, "Rerun when: the architecture moves"). Deleting the whole
    directory would destroy someone's real investigation. Only the two
    files call_map_data's own scan writes are removed here."""
    for name in (".review", ".briefing"):
        shutil.rmtree(path / name, ignore_errors=True)
    for name in ("call_map.json", "call_map.md"):
        (path / ".context" / name).unlink(missing_ok=True)


@pytest.fixture(scope="session")
def target_repo(pytestconfig: pytest.Config, request: pytest.FixtureRequest) -> Path:
    raw = pytestconfig.getoption("--target-repo")
    if not raw:
        raise pytest.UsageError("pass --target-repo PATH: a git repo with uncommitted changes for the app to review")
    path = Path(raw).resolve()
    if not (path / ".git").exists():
        raise pytest.UsageError(f"--target-repo {path} is not a git repository (no .git found)")
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=path, capture_output=True, text=True)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=path, capture_output=True, text=True
    )
    if not diff.stdout.strip() and not untracked.stdout.strip():
        raise pytest.UsageError(
            f"--target-repo {path} has no uncommitted changes (git diff vs HEAD is empty) — "
            "there is nothing for the app to review. Make some changes there first."
        )
    # Session-boundary cleanup (setup AND teardown, via the finalizer): this
    # suite treats target_repo as a real external project, not a scratch
    # fixture it owns — it should never leave that repo dirtier than it
    # found it, on either a clean run or a killed one. See
    # _remove_app_artifacts's own docstring for why these two dirs
    # specifically matter here.
    _remove_app_artifacts(path)
    request.addfinalizer(lambda: _remove_app_artifacts(path))
    return path


@pytest.fixture(scope="session")
def total_hunks(target_repo: Path) -> int:
    """How many review steps the app will show for target_repo's current
    diff — computed the same way app/services/diff_service.py's
    get_review_hunks does (git diff HEAD --unified=3, one hunk per '@@'
    line, plus one per untracked file), so the walk in test_live_review.py
    has a real termination bound instead of guessing from #next-btn's
    enabled state.

    That guess doesn't work here: unlike the synthetic suites' RepoSpec,
    there is no oracle handed to this suite up front, and — confirmed live
    — #next-btn does NOT disable at the last hunk (nextBtn's click handler
    unconditionally disables-then-re-enables on any server response; there
    is no "is this the last hunk" gate in static/js/review-flow.js). Clicking Next
    past the end still gets a response and re-enables the button, so a
    loop that waits for it to go disabled hangs until its own timeout."""
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--no-color", "--unified=3"],
        cwd=target_repo,
        capture_output=True,
        text=True,
    )
    tracked = sum(1 for line in diff.stdout.splitlines() if line.startswith("@@"))
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=target_repo, capture_output=True, text=True
    )
    untracked_count = len([line for line in untracked.stdout.splitlines() if line.strip()])
    return tracked + untracked_count


@pytest.fixture(scope="session")
def call_map_data(target_repo: Path, total_hunks: int) -> dict | None:
    """Runs the call-map skill's scanner (.claude/skills/call-map/
    scan_calls.py — a pure `ast` scanner, no model call) against
    target_repo and returns the parsed `.context/call_map.json`, or None
    if the repo has nothing to offer it (not Python, or no symbol has a
    recorded caller — scan_calls.py refuses to write an empty map rather
    than leave a misleading skeleton behind; see its own module
    docstring).

    This is the one place in this whole suite with a real oracle instead
    of an LLM judge's opinion — see test_live_dependency_accuracy.py,
    which cross-references this against target_repo's current diff to
    find a real function whose real callers are known, then checks a
    reply about it against that ground truth rather than just against
    plausibility.

    Takes `total_hunks` purely for ordering — never read, just resolved
    first — same idiom as page/page_with_ws's explicit
    `_clean_persisted_review_state` request below. Confirmed live: this
    fixture's own writes (call_map.json, call_map.md) are untracked files,
    same as .review/.briefing (see _remove_app_artifacts), and
    total_hunks counts every untracked file as a hunk. Writing them before
    total_hunks had run once inflated it from 8 to 10 — narrated_walk
    trusts that number as its own loop bound, so a wrong count there is a
    wrong walk, not just a late one. Ordering this fixture after
    total_hunks keeps that count accurate regardless of when this
    fixture's writes actually land.

    Session-scoped: static analysis, not a model call, so this is cheap,
    but still real subprocess + parse work not worth repeating per test.
    Written into target_repo's real `.context/` — cleaned up at session
    end by target_repo's own finalizer (see _remove_app_artifacts, which
    removes only call_map.json/.md, never a hand-authored
    project_overview alongside it)."""
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


@pytest.fixture(autouse=True)
def _clean_persisted_review_state(target_repo: Path) -> None:
    """Same reason as the sibling suites': without this, one test's
    "mark reviewed" (persisted to {target_repo}/.review/session_state.json)
    would silently carry into the next test's fresh connection. Cleaned
    before every test rather than only at session end, so a run that stops
    partway doesn't leave the real target repo with review state next to
    its working tree."""
    review_dir = target_repo / ".review"
    (review_dir / "session_state.json").unlink(missing_ok=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def app_dir(tmp_path_factory) -> Path:
    """A full copy of app/, static/, and run.py — see module docstring for
    why this is a copy rather than an in-place patch of the real project's
    own config.yaml."""
    dest = tmp_path_factory.mktemp("qa_agent_live_app")
    shutil.copytree(REPO_ROOT / "app", dest / "app")
    shutil.copytree(REPO_ROOT / "static", dest / "static")
    shutil.copy2(REPO_ROOT / "run.py", dest / "run.py")
    return dest


@pytest.fixture(scope="session")
def app_server(app_dir: Path, target_repo: Path, request: pytest.FixtureRequest):
    """A real `python run.py` subprocess, pointed at target_repo, on a free
    port. Leaves debug.capture_llm_calls at its config.yaml default
    (false) — unlike the sibling suites, this one isn't pairing captured
    prompts against a known-good spec, so there's nothing here that reads
    them; no reason to write whole-conversation dumps for someone's real
    repo content by default."""
    port = _free_port()
    config_path = app_dir / "app" / "config.yaml"
    original = config_path.read_text(encoding="utf-8")
    patched = original.replace('repo_path: "."', f'repo_path: "{target_repo.as_posix()}"')
    if patched == original:
        raise AssertionError(
            'app/config.yaml\'s repo_path: "." line was not found — '
            "config.yaml's shape may have changed; update this fixture to match."
        )
    with_port = patched.replace("port: 8765", f"port: {port}")
    if with_port == patched:
        raise AssertionError(
            "app/config.yaml's server port: 8765 line was not found — "
            "config.yaml's shape may have changed; update this fixture to match."
        )
    config_path.write_text(with_port, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=app_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Otherwise run.py opens a real browser tab as a second WS client,
        # which cancels the in-flight LLM work this suite is waiting on —
        # see qa_agent/conftest.py's app_server for the reproduced case.
        env={**os.environ, "REVIEW_NO_BROWSER": "1"},
    )
    log_tail: collections.deque[str] = collections.deque(maxlen=200)

    def _drain_stdout() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            log_tail.append(line)

    threading.Thread(target=_drain_stdout, daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"app server exited early:\n{''.join(log_tail)}")
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    else:
        raise TimeoutError(f"app server did not become ready at {url}\n{''.join(log_tail)}")

    try:
        yield url
    finally:
        if request.session.testsfailed:
            print(f"\n----- app server log (last {len(log_tail)} lines) -----")
            print("".join(log_tail), end="")
            print("----- end app server log -----")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _suppress_auto_tour(pg) -> None:
    """Kept in sync with the identical helper in the sibling suites — see
    qa_agent/conftest.py's copy for the full explanation. Every page this
    suite creates is a fresh browser with empty localStorage, so without
    this the guided tour's full-viewport overlay would auto-fire mid-test."""
    pg.add_init_script("try { localStorage.setItem('ai_pear_review_tour_seen', '1'); } catch (e) {}")
    # The app now explains a hunk only when asked (the Explain button); these
    # suites exercise narration, so they pin "Explain changes: Automatically".
    pg.add_init_script("try { localStorage.setItem('ai_pear_review_auto_narrate', '1'); } catch (e) {}")


@pytest.fixture
def page(app_server: str, playwright, _clean_persisted_review_state):
    browser = playwright.chromium.launch()
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(pg)
    pg.goto(app_server, wait_until="load")
    pg.wait_for_selector("#status-bar span", timeout=15000)
    yield pg
    browser.close()


@pytest.fixture(scope="session")
def judge_model_config() -> dict:
    """base_url/model/num_ctx for qa_agent's semantic judge, read from the
    REAL app/config.yaml (not app_dir's patched copy — repo_path/port are
    the only lines that copy's own patch touches, so reading the real file
    here is simplest). Duplicated from the sibling suites' identical
    fixture rather than shared — see qa_agent/conftest.py's own copy for
    the full reasoning (keeps the judge targeting whatever model
    config.yaml actually configures, never a drifting constant)."""
    config_text = (REPO_ROOT / "app" / "config.yaml").read_text(encoding="utf-8")
    block_match = re.search(r"^\s*ollama:\s*\n((?:^[ \t]{4,}.*\n?)+)", config_text, re.MULTILINE)
    if not block_match:
        raise AssertionError(
            "app/config.yaml's conversation.ollama: block was not found — "
            "config.yaml's shape may have changed; update judge_model_config to match."
        )
    block = block_match.group(1)
    base_url_match = re.search(r"base_url:\s*(\S+)", block)
    model_match = re.search(r"model:\s*(\S+)", block)
    if not base_url_match or not model_match:
        raise AssertionError(
            "app/config.yaml's conversation.ollama: block is missing base_url/model — "
            "config.yaml's shape may have changed; update judge_model_config to match."
        )
    config = {"base_url": base_url_match.group(1), "model": model_match.group(1)}
    num_ctx_match = re.search(r"num_ctx:\s*(\d+)", block)
    if num_ctx_match:
        config["num_ctx"] = int(num_ctx_match.group(1))
    return config
