"""Shared pytest fixtures for the qa_agent UI regression suite.

Isolation (see the plan this was built from): this launches `run.py` as a
real external subprocess — running from a full copy of app/+static/+
run.py under pytest's own tmp dir, never the real project's own files
(see app_copy_dir) — and talks to it only via HTTP/WebSocket + a real
browser, the same way any external client would. It never imports
anything from app.*, and the app never imports qa_agent.

Test isolation note: `app_server` and `scratch_repo` are session-scoped
(starting the server takes real seconds — not worth paying per test).
Each test still gets a *fresh in-memory Session* for free, because the
app builds a brand-new Session per WebSocket connection with no in-memory
reattachment (see app/server.py's websocket_endpoint) — conversation
history never leaks between tests just by virtue of each test's `page`
fixture opening a new connection. What does NOT reset automatically:

- The scratch git repo's working-tree contents (if a test's Act Now flow
  actually confirms a write) — tests that perform a real disk write
  should depend on `reset_scratch_repo` to restore it afterward.
- Review progress persisted to disk (app/services/session_store.py):
  review_started/review_ended, reviewed-hunk marks, and queued review
  comments are now written to `{scratch_repo}/.review/session_state.json`
  and *reloaded* by every new connection — deliberately, that's the whole
  feature (see app/server.py's module docstring). Since `scratch_repo` is shared
  session-scoped state, without cleanup one test's "mark this hunk
  reviewed" (or worse, an auto-triggered review_ended once the scratch
  repo's single hunk hits 100%) would silently carry into every later
  test. `_clean_persisted_review_state` below removes that file before
  every test runs, autouse, so every test still starts from a genuinely
  fresh review regardless of what an earlier test left behind.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

REPO_ROOT = Path(__file__).resolve().parent.parent

# qa_agent/generated/ is a deliberately separate suite (see its own
# conftest, and README.md's "The generated-repo suite"): it runs against a
# seeded, generated repo rather than the fixed scratch_repo below, and is
# meant to be invoked on its own — `pytest qa_agent/generated/`. Excluded
# from `pytest qa_agent/` so that command keeps meaning exactly what it
# has always meant: the deterministic regression suite, where a failure
# means the app broke rather than "a test assumed one repo shape". It also
# keeps the generated suite's own --repo-seed option out of this one's
# command line, where it would do nothing.
#
# qa_agent/live/ is excluded for the same reason: it drives the app against
# a real external repo's real uncommitted changes (see its own conftest),
# takes real minutes against a real local model, depends on a --target-repo
# path existing on this machine, and is meant to be invoked on its own —
# `pytest qa_agent/live/`.
collect_ignore = ["generated", "live"]

# The one predictable, uncommitted diff every test starts from: one small
# function added to a freshly-committed one-function file. Deterministic
# and independent of whatever's actually uncommitted in this project's own
# working tree at test time.
_SAMPLE_INITIAL = 'def greet(name):\n    return f"Hello, {name}!"\n'
_SAMPLE_MODIFIED = (
    'def greet(name):\n    return f"Hello, {name}!"\n\ndef farewell(name):\n    return f"Goodbye, {name}."\n'
)


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


# A second tracked file, committed once and never touched again — the one
# thing "changed files only" mode structurally can't have: something for
# "All files" explore mode (see test_explore_mode.py) to list and open
# that genuinely has no diff hunk. Never modified after the initial commit
# below, so every existing test's "there's exactly N hunks"/"exactly one
# file" assumption (all built against sample.py alone) stays true. Nested
# under pkg/ rather than the repo root — a real folder to collapse/expand
# (see test_file_tree.py), still zero hunks regardless of its path.
_UNCHANGED_CONTENT = 'def unchanged():\n    return "never touched by any test"\n'

# Two more always-unchanged tracked files, in two more folders — the
# scratch repo used to have exactly one folder (pkg/) and one unchanged
# file, which is enough for test_file_tree.py's own collapse/expand
# checks but too thin for a scenario that wants to click between several
# folders/files (see qa_agent/scenarios/). Safe to add unconditionally to
# every test's scratch_repo, not gated behind richer_scratch_repo below:
# these never carry a diff, so they can't add a hunk and can't disturb any
# existing "exactly N hunks" assumption — they only ever add to the
# *folder tree*, not the *changed-files* list. Distinct names/folders from
# pkg/unchanged.py on purpose (test_file_tree.py's own locators filter by
# the exact text "pkg"/"unchanged.py", so neither collides with them).
_HELPERS_FILE_PATH = "src/helpers.py"
_HELPERS_CONTENT = "def format_name(name):\n    return name.strip().title()\n"
_NOTES_FILE_PATH = "docs/notes.md"
_NOTES_CONTENT = "# Notes\n\nScratch notes for the test repo. Never modified by any test.\n"

# A fourth tracked file, committed with zero diff by default (same trick as
# _UNCHANGED_CONTENT above) but — unlike pkg/unchanged.py, which must never
# be touched — this one exists specifically to be temporarily modified by
# richer_scratch_repo below, for scenarios that need real multi-file/
# multi-hunk navigation. process()/summarize() are far enough apart (two
# unchanged functions between them) that a diff against _FEATURE_MODIFIED
# lands as two separate hunks, not one merged one — verified live via
# `git diff` while building this fixture; pad the gap further if a future
# git version's default context ever merges them again.
_FEATURE_FILE_PATH = "feature.py"
_FEATURE_INITIAL = (
    "def process(data):\n"
    "    return data\n"
    "\n"
    "\n"
    "def validate(data):\n"
    "    return bool(data)\n"
    "\n"
    "\n"
    "def normalize(data):\n"
    "    return data\n"
    "\n"
    "\n"
    "def helper(data):\n"
    "    return data\n"
    "\n"
    "\n"
    "def summarize(data):\n"
    "    return str(data)\n"
)
_FEATURE_MODIFIED = (
    "def process(data):\n"
    "    if data is None:\n"
    '        raise ValueError("data required")\n'
    "    return data\n"
    "\n"
    "\n"
    "def validate(data):\n"
    "    return bool(data)\n"
    "\n"
    "\n"
    "def normalize(data):\n"
    "    return data\n"
    "\n"
    "\n"
    "def helper(data):\n"
    "    return data\n"
    "\n"
    "\n"
    "def summarize(data):\n"
    '    return f"summary: {data}"\n'
)

# A fifth tracked file, also opt-in-modified by richer_scratch_repo —
# lives in the SAME folder (src/) as the always-unchanged _HELPERS_FILE_PATH
# above, deliberately: a folder with one changed file sitting next to one
# unchanged one is exactly the mixed case a file-tree scenario wants to
# click through, and feature.py alone (at the repo root) can't exercise
# "a folder containing a mix," only "folders exist at all."
_UTILS_FILE_PATH = "src/utils.py"
_UTILS_INITIAL = (
    "def load_config():\n    return {}\n\n\ndef validate_config(config):\n    return isinstance(config, dict)\n"
)
_UTILS_MODIFIED = (
    "def load_config():\n"
    '    return {"debug": False}\n'
    "\n\n"
    "def validate_config(config):\n"
    "    return isinstance(config, dict)\n"
)


def _seed_scratch_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    sample = repo / "sample.py"
    sample.write_text(_SAMPLE_INITIAL, encoding="utf-8")
    unchanged = repo / "pkg" / "unchanged.py"
    unchanged.parent.mkdir(parents=True, exist_ok=True)
    unchanged.write_text(_UNCHANGED_CONTENT, encoding="utf-8")
    helpers = repo / _HELPERS_FILE_PATH
    helpers.parent.mkdir(parents=True, exist_ok=True)
    helpers.write_text(_HELPERS_CONTENT, encoding="utf-8")
    notes = repo / _NOTES_FILE_PATH
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(_NOTES_CONTENT, encoding="utf-8")
    feature = repo / _FEATURE_FILE_PATH
    feature.write_text(_FEATURE_INITIAL, encoding="utf-8")
    utils = repo / _UTILS_FILE_PATH
    utils.parent.mkdir(parents=True, exist_ok=True)
    utils.write_text(_UTILS_INITIAL, encoding="utf-8")
    # Mirrors the real project's own .gitignore for these two directories —
    # without it, a Finish Review/briefing-cache test would leave an
    # untracked file behind that the app's own untracked-file review
    # feature would then present as an extra synthetic hunk, breaking
    # every other test's "there's exactly N hunks" assumptions.
    (repo / ".gitignore").write_text(".review/\n.briefing/\n", encoding="utf-8")
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "qa-agent@example.com")
    _run_git(repo, "config", "user.name", "QA Agent")
    _run_git(
        repo,
        "add",
        "sample.py",
        "pkg/unchanged.py",
        _HELPERS_FILE_PATH,
        _NOTES_FILE_PATH,
        _FEATURE_FILE_PATH,
        _UTILS_FILE_PATH,
        ".gitignore",
    )
    _run_git(repo, "commit", "-q", "-m", "initial")
    sample.write_text(_SAMPLE_MODIFIED, encoding="utf-8")


@pytest.fixture(scope="session")
def scratch_repo(tmp_path_factory) -> Path:
    """A small, throwaway git repo — created fresh under pytest's own tmp
    dir, never inside this actual project — with one predictable
    uncommitted diff for the app under test to present."""
    repo = tmp_path_factory.mktemp("qa_agent_scratch") / "scratch_repo"
    _seed_scratch_repo(repo)
    return repo


@pytest.fixture
def reset_scratch_repo(scratch_repo: Path):
    """Depend on this in any test whose flow performs a real disk write
    (Act Now confirm, in particular) — restores sample.py to the one
    predictable uncommitted diff every other test also expects, so a
    write in one test can't change what a later test sees.
    `reset --hard` discards staged/unstaged changes back to the one
    'initial' commit; then the standard uncommitted diff is rewritten."""
    yield
    _run_git(scratch_repo, "reset", "--hard", "HEAD")
    (scratch_repo / "sample.py").write_text(_SAMPLE_MODIFIED, encoding="utf-8")


@pytest.fixture
def richer_scratch_repo(scratch_repo: Path) -> Path:
    """Opt-in only — every existing test (and most new ones) gets the
    plain single-hunk scratch_repo above; several existing tests
    explicitly depend on that being exactly one hunk in one file (see
    test_accessibility.py, test_review_persistence.py,
    test_review_session_start.py, test_md_preview.py's own docstring
    explaining why it deliberately does NOT add a second hunk itself).
    This fixture layers two more modified files on top of the same shared
    scratch_repo — feature.py (repo root, two separate hunks — see
    _FEATURE_MODIFIED above) and src/utils.py (one hunk, sitting in the
    same src/ folder as the always-unchanged src/helpers.py) — for
    scenarios that need real multi-file/multi-folder/multi-hunk
    navigation: switching files across folders mid-conversation,
    revisiting one of several hunks, clicking through a folder that mixes
    changed and unchanged files. Total under this fixture: 4 hunks across
    3 files, in 2 folders (repo root + src/) plus the always-present
    zero-diff pkg/ and docs/. Restores both files to their committed
    (zero-diff) state afterward so no test that runs after — including
    another one in the same file that doesn't request this fixture — ever
    sees an extra hunk by accident."""
    (scratch_repo / _FEATURE_FILE_PATH).write_text(_FEATURE_MODIFIED, encoding="utf-8")
    (scratch_repo / _UTILS_FILE_PATH).write_text(_UTILS_MODIFIED, encoding="utf-8")
    yield scratch_repo
    _run_git(scratch_repo, "checkout", "--", _FEATURE_FILE_PATH, _UTILS_FILE_PATH)


@pytest.fixture(autouse=True)
def _clean_persisted_review_state(scratch_repo: Path) -> None:
    """Removes any review state a previous test persisted (see
    app/services/session_store.py) before this one runs. Autouse — every
    test in this suite gets this for free, not just ones that remember to
    request it, since `scratch_repo` is shared/session-scoped and a stale
    review_started/review_ended/reviewed-mark left by an earlier test
    would otherwise silently change what a completely unrelated later
    test's fresh connection sees. `page`/`page_with_ws` also request this
    explicitly (see below) so pytest resolves it before their own body
    runs, not just via autouse ordering."""
    (scratch_repo / ".review" / "session_state.json").unlink(missing_ok=True)


def _wait_for_server(proc: subprocess.Popen, log_tail: collections.deque[str], timeout: float) -> str:
    """Waits for the child to bind and returns the URL it ACTUALLY bound.

    Never assume APP_URL: run.py falls forward to the next free port when
    the configured one is taken (see its find_free_port). With a hardcoded
    URL, a dev server left running on 8765 answers the readiness probe
    instantly, the suite drives THAT server for the whole run, and the one
    it launched sits idle on 8766 — so tests silently run against the real
    repo, saving settings into it, while every assertion still passes. That
    happened here: a `python run.py` left over from the day before turned
    the whole suite into a live-fire run against the project itself.

    Reads log_tail (kept current by app_server's drain thread) rather than
    proc.stdout directly — that stream has exactly one reader for the
    server's whole lifetime, to avoid the pipe-buffer deadlock this fixture
    used to hit (see app_server)."""
    deadline = time.time() + timeout
    url = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"app server exited early (code {proc.returncode}):\n{''.join(log_tail)}")
        if url is None:
            # uvicorn's own startup line, not run.py's "Opening ..." — it is
            # printed after the bind succeeds, so it can't name a port the
            # server then failed to take.
            for line in list(log_tail):
                found = re.search(r"Uvicorn running on (http://\S+)", line)
                if found:
                    url = found.group(1).rstrip("/")
                    break
        if url is not None:
            try:
                urllib.request.urlopen(url, timeout=1)
                return url
            except Exception:
                pass
        time.sleep(0.3)
    raise TimeoutError(f"app server did not become ready within {timeout}s (bound url: {url}):\n{''.join(log_tail)}")


@pytest.fixture(scope="session")
def app_copy_dir(tmp_path_factory) -> Path:
    """A full copy of app/, static/, and run.py under pytest's own tmp
    dir. Deliberately a copy rather than patching the real project's
    app/config.yaml in place and restoring it afterward: a save/restore
    can't survive a hard kill of the test process (no Python
    finally/atexit/signal handler runs on SIGKILL) — this actually
    happened once while building this suite and left the real
    config.yaml pointing at a deleted temp path. A copy has no such
    failure mode: the real project's files are never written to at all,
    regardless of how the test run ends."""
    dest = tmp_path_factory.mktemp("qa_agent_app_copy")
    shutil.copytree(REPO_ROOT / "app", dest / "app")
    shutil.copytree(REPO_ROOT / "static", dest / "static")
    shutil.copy2(REPO_ROOT / "run.py", dest / "run.py")
    return dest


@pytest.fixture(scope="session")
def app_server(app_copy_dir: Path, scratch_repo: Path, request: pytest.FixtureRequest):
    """Patches the *copy's* config.yaml — repo_path to the scratch repo,
    and debug.capture_llm_calls on — then launches `python run.py` as a
    real subprocess from that copy (external process, not an in-process
    import — see module docstring). Never touches the real project's
    app/config.yaml, so capture stays off for every non-test run: that's
    the whole reason it's a config flag rather than always-on behavior
    (see config.yaml's own comment — the dumps hold whole files and whole
    conversations and nothing prunes them)."""
    config_path = app_copy_dir / "app" / "config.yaml"
    original_config = config_path.read_text(encoding="utf-8")
    patched = original_config.replace('repo_path: "."', f'repo_path: "{scratch_repo.as_posix()}"')
    if patched == original_config:
        raise AssertionError(
            'app/config.yaml\'s repo_path: "." line was not found — '
            "config.yaml's shape may have changed; update this fixture to match."
        )
    with_capture = patched.replace("capture_llm_calls: false", "capture_llm_calls: true")
    if with_capture == patched:
        raise AssertionError(
            "app/config.yaml's debug.capture_llm_calls: false line was not found — "
            "config.yaml's shape may have changed; update this fixture to match."
        )
    # Act Now runs through a coding agent; the suite uses the scripted fake
    # from tests/ so its Act Now tests are deterministic and need no Cline.
    fake_agent = (REPO_ROOT / "tests" / "fake_acp_agent.py").as_posix()
    with_agent = with_capture.replace("  agent: none  # none | cline", "  agent: cline").replace(
        'command: ["cline", "--acp"]', f"command: ['{Path(sys.executable).as_posix()}', '{fake_agent}', 'comment']"
    )
    if with_agent.count("agent: cline") != 1 or "fake_acp_agent.py" not in with_agent:
        raise AssertionError(
            "app/config.yaml's harness: block was not found — "
            "config.yaml's shape may have changed; update this fixture to match."
        )
    config_path.write_text(with_agent, encoding="utf-8")
    # The app reads the agent's provider, model and key from Cline's own
    # settings file (harness_service.resolve_agent_env). Point it at a fixture,
    # never the developer's real one — that holds real keys, and would make the
    # suite's settings panel depend on whichever model this machine happens to use.
    cline_settings = app_copy_dir / "cline_providers.json"
    cline_settings.write_text(
        json.dumps(
            {
                "lastUsedProvider": "anthropic",
                "providers": {"anthropic": {"settings": {"model": "claude-sonnet-5", "apiKey": "test-key-not-real"}}},
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=app_copy_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Without this run.py opens the UI in the real default browser, which
        # connects as a second WebSocket client. The server cancels the
        # previous client's in-flight LLM work on a new connection, so that
        # tab cancels the very briefing/narration these tests wait on — a
        # real, reproduced source of flakiness, not a theoretical one.
        env={**os.environ, "REVIEW_NO_BROWSER": "1", "CLINE_PROVIDER_SETTINGS_PATH": str(cline_settings)},
    )
    # Uvicorn logs a line per request — over ~27 tests' worth of
    # reconnects that adds up. subprocess.PIPE has a small OS-level
    # buffer (~64KB on Windows); once it fills, the child's next write to
    # stdout blocks until someone reads the pipe, which freezes the
    # *entire* server process (a synchronous log call blocks the whole
    # single-threaded asyncio event loop with it) — not a git/Ollama
    # slowdown at all, a plain subprocess-pipe deadlock. This actually
    # happened while building this suite: the server went totally
    # unresponsive after a consistent volume of requests regardless of
    # which tests ran, which is the signature of this exact bug, not a
    # content-dependent one. A background thread continuously draining
    # the pipe (kept bounded, not accumulated forever) fixes it.
    log_tail: collections.deque[str] = collections.deque(maxlen=200)

    def _drain_stdout() -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            log_tail.append(line)

    threading.Thread(target=_drain_stdout, daemon=True).start()

    try:
        yield _wait_for_server(proc, log_tail, timeout=20)
    finally:
        # Until now log_tail was only ever read by _wait_for_server, so a
        # test that timed out against a live server discarded the one
        # record of what that server was doing — which is most of why the
        # Act Now hang could be described as "nothing logged server-side"
        # when the server was in fact logging. Printing it only on failure
        # keeps green runs quiet; run_llm's llm[N] start/ok/cancelled
        # lines are the ones worth reading here.
        if request.session.testsfailed:
            print(f"\n----- app server log (last {len(log_tail)} lines) -----")
            print("".join(log_tail), end="")
            print("----- end app server log -----")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def llm_capture_dir(app_copy_dir: Path) -> Path:
    """Where the app-under-test writes its per-call prompt/response dumps
    (see config.yaml's debug.capture_dir, enabled by app_server above).
    The app resolves that relative path against its own working directory,
    which is app_copy_dir — pytest's tmp dir — so the captures live and die
    with the session rather than accumulating anywhere real.

    Depends on app_copy_dir rather than app_server so a test can request it
    without implying anything about server startup ordering; the directory
    simply doesn't exist until the first LLM call writes into it, which
    qa_agent/llm_capture.py already treats as "no captures yet"."""
    return app_copy_dir / ".llm_calls"


@pytest.fixture(scope="session")
def judge_model_config() -> dict:
    """base_url/model for qa_agent/llm_client.py's judge, read straight out
    of the REAL app/config.yaml's conversation.ollama: block (not the copy
    app_server patches — repo_path is the only line that copy's own patch
    ever touches, so reading the real file here is simplest and avoids
    re-parsing the copy for values that are identical either way). Every
    LLMClient() built by this suite's tests should be constructed with
    base_url=/model= from this fixture rather than llm_client.py's own
    DEFAULT_BASE_URL/DEFAULT_MODEL constants (those are a fallback for code
    outside the pytest suite only — see that module's docstring) — this is
    what keeps the judge targeting whatever model the app-under-test is
    actually configured to run, instead of a constant that can silently
    drift out of sync with config.yaml (which is exactly what happened
    before this fixture existed: DEFAULT_MODEL named llama3.2:3b long after
    config.yaml had moved on to qwen2.5-coder:7b).

    A plain line-scrape, not a real YAML parse — this suite has no other
    reason to depend on pyyaml, and app_server's own config patch above
    already sets the precedent of treating config.yaml as text rather than
    pulling in a parser for one value. Matches the "ollama:" sub-block
    specifically (4+-space-indented lines following it) so this can never
    accidentally pick up the sibling "anthropic:" block's own "model:" key,
    which sits at the same 2-space indent as "ollama:" itself and so falls
    outside the match."""
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
    # num_ctx matters here for speed, not correctness — and it matters a
    # lot. It's a load-time parameter in Ollama, so a judge asking for a
    # different context size than the app-under-test makes the model
    # reload on every switch between them, which this suite does
    # constantly (one app call, then k judge votes, then the next). See
    # llm_client.py's own comment for the measured ~9x per-call penalty.
    num_ctx_match = re.search(r"num_ctx:\s*(\d+)", block)
    if num_ctx_match:
        config["num_ctx"] = int(num_ctx_match.group(1))
    return config


def _suppress_auto_tour(pg) -> None:
    """Call this immediately after every `browser.new_page(...)` in this
    suite — the shared `page`/`page_with_ws` fixtures below already do,
    but several files here deliberately build their own raw page instead
    (test_review_session_start.py, test_explore_mode.py,
    test_review_persistence.py, test_review_reset.py — anything that needs
    the PRE-Start-Review state `page`/`page_with_ws` skip past) and each of
    those needs this call too. There is no single choke point that could
    apply it automatically: Playwright has no browser-level init-script
    hook, only per-page/per-context ones. If you add a new file with its
    own `browser.new_page(...)`, call this right after it.

    Pre-seeds the guided tour's "seen" flag via an init script — one
    that runs before the app's own top-level code on every navigation this
    page makes, per Playwright's add_init_script contract.

    Without this, every test here gets a *fresh* browser with empty
    localStorage (see page/page_with_ws's own docstrings on why: per-test
    isolation), which is exactly the condition the guided tour's
    maybeAutoStartTour uses to decide "never seen it, show it" — so it
    would auto-fire about 600ms into essentially every test in this whole
    suite. Its highlight overlay is full-viewport and sits above
    everything (z-index 1000+), so a test's own page.click() calls after
    that point would intercept on the tour instead of the real control —
    the same class of failure this session already diagnosed once for
    run.py's auto-opened browser tab, just for a UI overlay instead of a
    second WS client. Seeding the flag is the same idea as
    REVIEW_NO_BROWSER there: neutralize a first-run behavior that exists
    for a real user, not for an automated one running hundreds of times.
    """
    pg.add_init_script("try { localStorage.setItem('ai_pear_review_tour_seen', '1'); } catch (e) {}")
    # The app now explains a hunk only when asked (the Explain button); these
    # suites exercise narration, so they pin "Explain changes: Automatically".
    pg.add_init_script("try { localStorage.setItem('ai_pear_review_auto_narrate', '1'); } catch (e) {}")


def _start_review_if_gated(pg) -> None:
    """Clicks the toolbar's "Start Review" button (see review-flow.js's
    updateStartReviewBtn) if it's showing, so every existing test's
    assumption that the first hunk's narration is already available by the
    time `page`/`page_with_ws` return stays true under the review_started
    gate in app/handlers/narration.py (briefing/conversation now don't fire at all
    until this is clicked — see present_current_hunk). The button stays
    hidden (never becomes clickable, so this just times out and returns) in
    two legitimate cases, neither a bug here: no hunks to review (the "no
    changes found" error case), and degraded mode (no conversation agent
    configured)."""
    try:
        pg.click("#start-review-btn", timeout=5000)
    except PlaywrightTimeoutError:
        return
    pg.wait_for_selector("#transcript .turn.presenter, #transcript .turn.system", timeout=30000)


@pytest.fixture
def page(app_server: str, playwright, _clean_persisted_review_state):
    """Overrides pytest-playwright's default `page` fixture: launches its
    own fresh browser per test (rather than sharing one session-scoped
    `browser` across all ~27 tests) and returns a page already navigated
    to the running app, settled past the initial WebSocket handshake and
    past the "Start Review" button (see _start_review_if_gated) — the
    first hunk's narration is already showing by the time this yields,
    same as every test here assumed before the review_started
    gate existed. Explicitly depends on _clean_persisted_review_state
    (already autouse, but requesting it here too guarantees pytest
    resolves it before this fixture's own body runs, not just before the
    test function).

    The actual root cause of an earlier "every test after the Nth times
    out" run turned out to be a subprocess-pipe deadlock in app_server
    (fixed there), not browser state — but per-test browser isolation is
    a reasonable default in its own right (a little startup cost for one
    less category of cross-test state to reason about), so it stays.

    wait_until="load" rather than "networkidle": this app opens a
    long-lived WebSocket immediately on load, and Playwright's
    networkidle wait (0 in-flight connections for 500ms) is documented as
    unreliable for pages with persistent connections — hit exactly this
    as one-off flakiness while building this suite. wait_for_selector
    below is the real readiness signal anyway (the status bar only
    populates once service_status/review_progress arrive over the WS),
    so networkidle was never load-bearing here to begin with."""
    browser = playwright.chromium.launch()
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(pg)
    pg.goto(app_server, wait_until="load")
    pg.wait_for_selector("#status-bar span", timeout=15000)
    _start_review_if_gated(pg)
    yield pg
    browser.close()


@pytest.fixture
def page_with_ws(app_server: str, playwright, _clean_persisted_review_state):
    """Like `page`, but with WebSocket frame capture (see ws_capture.py)
    attached *before* navigation — Stage 2's judges need to observe WS
    message content (narration text, replies, Act Now's proposed diff),
    not just DOM state, and some of those messages (e.g. "presenting")
    arrive automatically the instant the connection opens, so capture
    has to be wired up before `goto`, not after."""
    from .ws_capture import WsFrames

    browser = playwright.chromium.launch()
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(pg)
    frames = WsFrames()
    frames.attach(pg)
    pg.goto(app_server, wait_until="load")
    pg.wait_for_selector("#status-bar span", timeout=15000)
    _start_review_if_gated(pg)
    yield pg, frames
    browser.close()


@pytest.fixture(scope="session", autouse=True)
def _truncate_findings_log():
    """Stage 2/3's judge verdicts append to findings.jsonl (see
    findings_log.py's record_finding) — truncate once per full suite run
    so it reflects only the current run, not an ever-growing history
    across every past invocation. autouse so this happens even if no
    Stage-2/3 test in this particular run ever writes to it."""
    from .findings_log import FINDINGS_PATH

    FINDINGS_PATH.write_text("", encoding="utf-8")
    yield


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Writes qa_agent/pytest_summary.json — a tiny per-test outcome +
    duration record — for aggregate_findings.py (Stage 4) to fold Stage 1's
    mechanical pass/fail counts into results.md alongside Stage 2/3's own
    findings.jsonl verdicts. Deliberately not pytest-json-report (a new
    dependency this suite has no other reason to need) — a core pytest hook
    plus the terminalreporter plugin's own already-collected `.stats`
    (built into every normal pytest run, no plugin install required) gives
    everything the aggregator actually needs. aggregate_findings.py treats
    a missing/unreadable file here as "no pytest data available" and still
    renders a findings-only report, so this hook failing to find the
    terminalreporter plugin (e.g. under -p no:terminalreporter) degrades
    gracefully rather than blocking the run."""
    from .findings_log import FINDINGS_PATH

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {}) if reporter else {}
    # Only "passed" needs the when == "call" filter, to dedupe: a normal
    # test produces up to 3 reports (setup/call/teardown), all "passed" if
    # nothing goes wrong, which would triple-count every green test
    # otherwise. "failed"/"error"/"skipped" don't have that problem the
    # same way — a fixture failure surfaces as pytest's own "error" bucket
    # with when in ("setup", "teardown") *specifically* (never "call", by
    # pytest's own categorization: a call-phase failure is "failed", not
    # "error") — restricting those to when == "call" too would silently
    # drop every fixture-setup failure from this summary entirely, which
    # defeats the point of recording it.
    outcomes = {
        outcome: [
            {"nodeid": report.nodeid, "duration": getattr(report, "duration", None)}
            for report in reports
            if outcome != "passed" or getattr(report, "when", "call") == "call"
        ]
        for outcome, reports in stats.items()
        if outcome in ("passed", "failed", "error", "skipped")
    }
    summary_path = FINDINGS_PATH.parent / "pytest_summary.json"
    summary_path.write_text(json.dumps(outcomes, indent=2), encoding="utf-8")
