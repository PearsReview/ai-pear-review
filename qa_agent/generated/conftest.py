"""Fixtures for the generated-repo suite.

Separate from qa_agent/conftest.py on purpose, and run as its own pytest
invocation. Two reasons:

1. **The two suites do different jobs.** The fixed scratch repo next door
   is a regression baseline: deterministic, and a failure there means the
   app broke. This one is a robustness probe: a failure here might instead
   mean a test made an assumption that only held for one repo shape.
   Merging them would make every red run ambiguous.

2. **Port 8765 is a session-scoped singleton.** qa_agent/conftest.py's
   `app_server` binds it for the whole session, so a second server in the
   same session would collide. Keeping this a separate invocation sidesteps
   that without having to make the port dynamic first.

A note on writing assertions here, which is the specific hazard of a
generated fixture: an assertion that reads a value from the spec and
compares it to something else derived from the same spec proves nothing —
it passes by construction. Every assertion below compares something the
*app* produced (the DOM, a WebSocket frame) against the spec. The spec is
the oracle; it must never be both sides of the comparison.
"""

from __future__ import annotations

import collections
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

from qa_agent.repo_gen import RepoSpec, generate_repo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# A fixed default keeps CI deterministic and makes a failure reproducible;
# --repo-seed=random is the point of the whole exercise, for local runs
# that want to actually vary the shape.
_DEFAULT_SEED = 1729


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--repo-seed",
        action="store",
        default=str(_DEFAULT_SEED),
        help="Seed for the generated test repo. An integer, or 'random' for a new shape each run. "
        "The chosen seed is printed at session start — pass it back to reproduce a failure.",
    )


@pytest.fixture(scope="session")
def repo_seed(pytestconfig: pytest.Config) -> int:
    raw = pytestconfig.getoption("--repo-seed")
    if str(raw).lower() == "random":
        import random

        return random.randrange(1, 10_000_000)
    try:
        return int(raw)
    except ValueError:
        # from None: the message already quotes the bad value, and int()'s own
        # "invalid literal" adds nothing for someone who mistyped a CLI flag.
        raise pytest.UsageError(f"--repo-seed must be an integer or 'random', got {raw!r}") from None


@pytest.fixture(scope="session")
def spec(tmp_path_factory, repo_seed: int) -> RepoSpec:
    """The generated repo, plus the truthful description of it that every
    assertion in this suite reads from."""
    root = tmp_path_factory.mktemp("qa_agent_generated") / "repo"
    generated = generate_repo(root, repo_seed)
    # Printed unconditionally: when a test fails, the first question is
    # always "what did the repo look like?", and re-running to find out
    # would generate a different one unless the seed is already known.
    print(f"\n--- generated repo (reproduce with --repo-seed={repo_seed}) ---")
    print(generated.describe())
    print("---")
    return generated


@pytest.fixture(autouse=True)
def _clean_persisted_review_state(spec: RepoSpec) -> None:
    """Same reason as the sibling suite's: the repo is session-scoped, so a
    review_started/reviewed mark left by one test would silently change
    what the next test's fresh connection sees."""
    (spec.root / ".review" / "session_state.json").unlink(missing_ok=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def app_dir(tmp_path_factory) -> Path:
    """A full copy of app/ + static/ + run.py under pytest's tmp dir.

    A copy rather than patching the real project's config.yaml in place:
    a save/restore can't survive a hard kill of the test process, and that
    would leave the real config pointing at a deleted temp path.
    """
    dest = tmp_path_factory.mktemp("qa_agent_generated_app")
    shutil.copytree(REPO_ROOT / "app", dest / "app")
    shutil.copytree(REPO_ROOT / "static", dest / "static")
    shutil.copy2(REPO_ROOT / "run.py", dest / "run.py")
    return dest


@pytest.fixture(scope="session")
def llm_capture_dir(app_dir: Path) -> Path:
    """Where the app-under-test dumps each LLM call's full prompt and
    response (config.yaml's debug.capture_dir, switched on by app_server).
    Resolved against the app's own working directory, so the dumps live and
    die with the pytest tmp dir rather than accumulating anywhere real."""
    return app_dir / ".llm_calls"


@pytest.fixture(scope="session")
def judge_model_config() -> dict:
    """base_url/model/num_ctx for the judge, read from the REAL
    app/config.yaml so the judge targets whatever model the app under test
    is actually running — a drifting constant here silently judges one
    model's output with another's.

    num_ctx matters for speed, not correctness, and matters a lot: it's a
    load-time parameter in Ollama, so a judge asking for a different
    context size than the app forces a model reload on every switch
    between them — which this suite does constantly.

    A line-scrape rather than a YAML parse, matching the sibling suite:
    the ollama: sub-block specifically, so the sibling anthropic: block's
    own model: key can never be picked up by accident.
    """
    config_text = (REPO_ROOT / "app" / "config.yaml").read_text(encoding="utf-8")
    block = re.search(r"^\s*ollama:\s*\n((?:^[ \t]{4,}.*\n?)+)", config_text, re.MULTILINE)
    if not block:
        raise AssertionError(
            "app/config.yaml's conversation.ollama: block was not found — "
            "config.yaml's shape may have changed; update judge_model_config to match."
        )
    body = block.group(1)
    base_url = re.search(r"base_url:\s*(\S+)", body)
    model = re.search(r"model:\s*(\S+)", body)
    if not base_url or not model:
        raise AssertionError("app/config.yaml's ollama: block is missing base_url/model")
    config = {"base_url": base_url.group(1), "model": model.group(1)}
    num_ctx = re.search(r"num_ctx:\s*(\d+)", body)
    if num_ctx:
        config["num_ctx"] = int(num_ctx.group(1))
    return config


@pytest.fixture(scope="session")
def app_server(app_dir: Path, spec: RepoSpec, request: pytest.FixtureRequest):
    """A real `python run.py` subprocess against the generated repo.

    Binds a free port rather than 8765, so this can run while the sibling
    suite (or a developer's own `python run.py`) is up.
    """
    port = _free_port()
    config_path = app_dir / "app" / "config.yaml"
    original = config_path.read_text(encoding="utf-8")
    patched = original.replace('repo_path: "."', f'repo_path: "{spec.root.as_posix()}"')
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
    # Test-only, and only in this copy: the real project's config.yaml keeps
    # capture_llm_calls false, so a normal `python run.py` never writes these.
    with_capture = with_port.replace("capture_llm_calls: false", "capture_llm_calls: true")
    if with_capture == with_port:
        raise AssertionError(
            "app/config.yaml's debug.capture_llm_calls: false line was not found — "
            "config.yaml's shape may have changed; update this fixture to match."
        )
    config_path.write_text(with_capture, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=app_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # run.py otherwise opens a real browser tab, which connects as a
        # second WebSocket client and cancels the in-flight LLM work this
        # suite is waiting on. Reproduced, not theoretical.
        env={**os.environ, "REVIEW_NO_BROWSER": "1"},
    )
    # Drained continuously: subprocess.PIPE's OS buffer is small, and once
    # full the server's next log write blocks its whole event loop.
    log_tail: collections.deque[str] = collections.deque(maxlen=200)
    threading.Thread(
        target=lambda: [log_tail.append(line) for line in proc.stdout] if proc.stdout else None,
        daemon=True,
    ).start()

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
    """Kept in sync with the identical helper in qa_agent/conftest.py —
    see that copy's docstring. Duplicated rather than imported across
    suites for the same reason this project's small local constants
    generally are (see e.g. ACT_NOW_TIMEOUT_MS): one tiny function with a
    local explanation, not a cross-suite import for its own sake. Every
    page this suite creates is a fresh browser with empty localStorage, so
    without this the guided tour would auto-fire mid-test here too."""
    pg.add_init_script("try { localStorage.setItem('ai_pear_review_tour_seen', '1'); } catch (e) {}")
    # The app now explains a hunk only when asked (the Explain button); these
    # suites exercise narration, so they pin "Explain changes: Automatically".
    pg.add_init_script("try { localStorage.setItem('ai_pear_review_auto_narrate', '1'); } catch (e) {}")


def _start_review_if_gated(pg) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        pg.click("#start-review-btn", timeout=5000)
    except PlaywrightTimeoutError:
        return
    pg.wait_for_selector("#transcript .turn.presenter, #transcript .turn.system", timeout=60000)


@pytest.fixture
def page(app_server: str, playwright, _clean_persisted_review_state):
    browser = playwright.chromium.launch()
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(pg)
    pg.goto(app_server, wait_until="load")
    pg.wait_for_selector("#status-bar span", timeout=15000)
    yield pg
    browser.close()


@pytest.fixture
def reviewing_page(page):
    """A page past the Start Review gate, with the first hunk presented."""
    _start_review_if_gated(page)
    return page


# A hunk's first narration is two chained model calls — a fresh briefing
# (nothing cached yet) and then the narration — measured at ~22s + ~15s on
# a CPU-only setup.
#
# 120s rather than the sibling suite's 60s because of a third cost that
# only the *first* narration of a session pays: if Ollama has unloaded the
# model (its keep_alive expires after a few idle minutes), the first call
# also pays a ~5GB load. Measured directly: 22s + 15s is comfortably
# inside 60s, and this suite still timed out at 60s having seen zero
# narrations, on a run that followed several idle minutes. The work is
# already slow and only pays this once, so a generous ceiling costs
# nothing on a healthy run and removes a failure mode that looks exactly
# like a real regression.
FRESH_NARRATION_TIMEOUT_MS = 120_000


@pytest.fixture(scope="session")
def narrated_hunks(app_server: str, playwright, spec: RepoSpec) -> list[dict]:
    """Walks the whole generated change once, collecting the `presenting`
    and `narration` frame for every hunk.

    Session-scoped because it is by far the most expensive thing in this
    suite: every hunk costs a briefing plus a narration, so a six-hunk seed
    is minutes of real model time. Every judge below reads this same
    collection rather than re-walking, which is the difference between one
    slow suite and an unusable one.

    Note what this buys over the sibling suite, which judges the first hunk
    of one fixed repo: here every hunk of a varied repo gets judged, and
    the hunks are deliberately similar to each other in shape while
    differing in file and function name — which is exactly the situation
    where a model starts attributing one file's code to another.
    """
    from qa_agent.ws_capture import WsFrames

    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    frames = WsFrames()
    frames.attach(page)
    try:
        page.goto(app_server, wait_until="load")
        page.wait_for_selector("#status-bar span", timeout=15000)
        page.click("#start-review-btn", timeout=10000)

        collected: list[dict] = []
        for _ in range(spec.total_hunks):
            # Order matters: wait for THIS hunk's narration first, then read
            # the newest `presenting`. WsFrames.wait_for returns the newest
            # *existing* match immediately, so asking for `presenting` first
            # and narration second would happily pair hunk 2's diff with
            # hunk 1's leftover narration. Waiting on the narration count
            # means the newest presenting is necessarily this hunk's — the
            # next one can't have arrived, since Next hasn't been clicked.
            narration = _wait_for_narration_number(frames, page, len(collected) + 1)
            presenting = frames.wait_for("presenting", page, timeout_ms=30_000)
            collected.append({"presenting": presenting, "narration": narration})

            next_button = page.locator("#next-btn")
            if not next_button.is_enabled():
                break
            next_button.click()
            page.wait_for_timeout(500)
        return collected
    finally:
        browser.close()


def _wait_for_narration_number(frames, page, n: int) -> dict:
    """Waits until the nth narration frame of the whole session exists.

    Counting rather than calling wait_for, which returns the newest
    existing match straight away — on hunk 2 that would hand back hunk 1's
    narration and every judge downstream would read the same text twice.
    The sibling suite hit this exact staleness trap and solved it the same
    way (see scenario_helpers.wait_for_new_frame).
    """
    deadline = time.time() + FRESH_NARRATION_TIMEOUT_MS / 1000
    while time.time() < deadline:
        narrations = [f for f in frames.frames if f.get("type") == "narration"]
        if len(narrations) >= n:
            return narrations[n - 1]["payload"]
        page.wait_for_timeout(500)
    raise TimeoutError(
        f"narration #{n} never arrived within {FRESH_NARRATION_TIMEOUT_MS}ms "
        f"(saw {len([f for f in frames.frames if f.get('type') == 'narration'])})"
    )
