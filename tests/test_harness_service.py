"""Act Now through a coding agent (app/services/harness_service.py over
acp_client.py), driven by tests/fake_acp_agent.py — a scripted ACP agent —
so the protocol, permission policy and preview/apply guarantees are checked
without Cline or a model."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.services import harness_service
from app.services.acp_client import AcpConnection
from app.services.harness_service import (
    ActNowRequest,
    AgentModel,
    HarnessError,
    ProposedChange,
    _build_sandbox,
    _collect_changes,
    _Turn,
    apply_changes,
    build_act_now_prompt,
    harness_status,
    is_read_only_command,
    resolve_agent_env,
    run_agent_edit,
    run_agent_research,
)
from app.services.settings_store import sanitize_harness

FAKE_AGENT = Path(__file__).with_name("fake_acp_agent.py")
SAMPLE = "def hello():\n    return 'hi'\n"


@pytest.fixture(autouse=True)
def _no_real_cline_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """resolve_agent_env reads Cline's own settings file — which holds real API
    keys. Never let a test read the developer's; point it at an empty path."""
    monkeypatch.setenv("CLINE_PROVIDER_SETTINGS_PATH", str(tmp_path / "no-cline-settings.json"))
    for name in ("CLINE_PROVIDER", "CLINE_MODEL", "CLINE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "sample.py").write_bytes(SAMPLE.encode())
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    (root / "notes.txt").write_text("untracked but not ignored\n", encoding="utf-8")
    return root


def _config(scenario: str, timeout: int = 30) -> dict:
    return {
        "agent": "cline",
        "timeout_seconds": timeout,
        "cline": {"command": [sys.executable, str(FAKE_AGENT), scenario]},
    }


def _run(repo: Path, scenario: str, cancel: threading.Event | None = None, timeout: int = 30):
    return run_agent_edit(_config(scenario, timeout), str(repo), "make the change", cancel or threading.Event())


def test_agent_edits_are_proposed_not_written(repo: Path):
    edit = _run(repo, "edit")

    by_path = {c.file_path: c for c in edit.changes}
    assert by_path["sample.py"] == ProposedChange("sample.py", SAMPLE, "# greeting helpers\n" + SAMPLE)
    assert by_path["new_module.py"] == ProposedChange("new_module.py", None, "VALUE = 1\n")
    assert by_path["notes.txt"].new_text is None  # an untracked file the agent deleted
    assert "build/out.txt" not in by_path  # gitignored output isn't part of the change
    assert edit.summary == "Added a comment."
    # The working tree is untouched until apply_changes.
    assert (repo / "sample.py").read_bytes() == SAMPLE.encode()
    assert not (repo / "new_module.py").exists()
    assert (repo / "notes.txt").exists()


def test_apply_writes_exactly_the_preview(repo: Path):
    edit = _run(repo, "edit")
    apply_changes(str(repo), edit.changes)

    assert (repo / "sample.py").read_text(encoding="utf-8") == "# greeting helpers\n" + SAMPLE
    assert (repo / "new_module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (repo / "notes.txt").exists()


def test_apply_refuses_everything_if_any_file_changed_since_preview(repo: Path):
    edit = _run(repo, "edit")
    (repo / "sample.py").write_text("edited by the reviewer meanwhile\n", encoding="utf-8")

    with pytest.raises(HarnessError, match=r"sample\.py"):
        apply_changes(str(repo), edit.changes)
    assert not (repo / "new_module.py").exists()  # all or nothing
    assert (repo / "notes.txt").exists()


def test_refine_starts_from_the_preview_and_replaces_it(repo: Path):
    first = _run(repo, "comment")
    refined = run_agent_edit(_config("comment"), str(repo), "refine it", threading.Event(), base=first.changes)

    # One change against the repo holding both edits, not a second change on
    # top of the first: applying it is still a single, checked write.
    added = "# Reviewed with Act Now\n"
    assert refined.changes == (ProposedChange("sample.py", SAMPLE, added * 2 + SAMPLE),)
    assert (repo / "sample.py").read_bytes() == SAMPLE.encode()
    apply_changes(str(repo), refined.changes)
    assert (repo / "sample.py").read_text(encoding="utf-8") == added * 2 + SAMPLE


def test_refine_refuses_if_the_repo_changed_since_the_preview(repo: Path):
    first = _run(repo, "comment")
    (repo / "sample.py").write_text("edited by the reviewer meanwhile\n", encoding="utf-8")

    with pytest.raises(HarnessError, match=r"sample\.py changed since the preview"):
        run_agent_edit(_config("comment"), str(repo), "refine it", threading.Event(), base=first.changes)


def test_act_now_prompt_carries_refinements_in_order():
    request = ActNowRequest("app.py", "lines 3-4", "x = 1", ("rename x",))
    assert "already been made" not in build_act_now_prompt(request)

    prompt = build_act_now_prompt(
        ActNowRequest("app.py", "lines 3-4", "x = 1", ("rename x", "call it count", "keep the comment"))
    )
    assert prompt.index("rename x") < prompt.index("already been made")
    assert prompt.index("- call it count") < prompt.index("- keep the comment")


def test_apply_refuses_paths_outside_the_repo(repo: Path):
    with pytest.raises(HarnessError, match="outside"):
        apply_changes(str(repo), (ProposedChange("../escape.py", None, "x\n"),))
    assert not (repo.parent / "escape.py").exists()


def test_agent_env_block_reaches_the_agent(repo: Path):
    config = _config("env")
    config["cline"]["env"] = {"FAKE_AGENT_SETTING": "from config.yaml"}
    edit = run_agent_edit(config, str(repo), "report", threading.Event())
    assert edit.summary == "from config.yaml"


def test_client_fs_writes_land_in_the_copy(repo: Path):
    edit = _run(repo, "fs_write")
    assert [c.file_path for c in edit.changes] == ["sample.py"]
    assert edit.changes[0].new_text == "x = 2\n"
    assert (repo / "sample.py").read_bytes() == SAMPLE.encode()


def test_client_fs_refuses_paths_outside_the_copy(repo: Path):
    (repo.parent / "secret.txt").write_text("nope", encoding="utf-8")
    assert _run(repo, "fs_outside").summary == "error"


@pytest.mark.parametrize(
    "scenario",
    [
        "execute",  # commands aren't confined to the copy
        "outside",  # an edit naming a file outside the copy
        "other_no_paths",  # an unclassified tool that says nothing about what it touches
    ],
)
def test_permission_is_rejected(repo: Path, scenario: str):
    edit = _run(repo, scenario)
    assert edit.summary == "outcome=no"
    assert edit.changes == ()


def test_permission_falls_back_to_cancelled_without_a_reject_option(tmp_path: Path):
    turn = _Turn(tmp_path.resolve())
    outcome = turn.on_request(
        "session/request_permission",
        {"toolCall": {"toolCallId": "t", "kind": "execute"}, "options": [{"optionId": "y", "kind": "allow_once"}]},
    )
    assert outcome == {"outcome": {"outcome": "cancelled"}}


def test_cancel_stops_the_agent_promptly(repo: Path):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(HarnessError, match="cancelled"):
        _run(repo, "hang", cancel=cancel)
    assert time.monotonic() - started < 10


def test_timeout_is_reported(repo: Path):
    with pytest.raises(HarnessError, match="within 1s"):
        _run(repo, "hang", timeout=1)


def test_agent_crash_reports_its_stderr(repo: Path):
    with pytest.raises(HarnessError, match="boom"):
        _run(repo, "crash")


def test_session_failure_names_the_sign_in_fix(repo: Path):
    with pytest.raises(HarnessError, match="cline auth"):
        _run(repo, "no_auth")


def test_close_kills_an_agent_that_ignores_stdin_closing(tmp_path: Path):
    conn = AcpConnection(
        [sys.executable, str(FAKE_AGENT), "slow_exit"], str(tmp_path), lambda m, p: None, lambda m, p: None
    )
    conn.request("initialize", {"protocolVersion": 1}, 30)
    conn.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 30)
    conn.request("session/prompt", {"sessionId": "sess_1", "prompt": []}, 30)
    started = time.monotonic()
    conn.close()
    assert conn._proc.poll() is not None
    assert time.monotonic() - started < 15


def test_repo_edits_during_the_run_are_not_overwritten(repo: Path, tmp_path: Path):
    sandbox = (tmp_path / "copy").resolve()
    sandbox.mkdir()
    snapshot = _build_sandbox(str(repo), sandbox)
    (sandbox / "sample.py").write_text("agent version\n", encoding="utf-8")
    (repo / "sample.py").write_text("reviewer version\n", encoding="utf-8")

    with pytest.raises(HarnessError, match="changed in the repo"):
        _collect_changes(str(repo), sandbox, snapshot)


def test_status_without_an_agent_explains_how_to_enable_act_now():
    status = harness_status({"agent": "none"})
    assert not status.available
    assert "Cline" in status.detail


def test_status_when_the_agent_is_not_installed():
    status = harness_status({"agent": "cline", "cline": {"command": ["definitely-not-installed-agent-xyz"]}})
    assert not status.available
    assert "npm i -g cline" in status.detail


def test_status_when_the_agent_is_installed():
    assert harness_status(_config("edit")).available


@pytest.mark.parametrize(
    "incoming,expected",
    [
        ({"harness": {"agent": "cline"}}, {"harness": {"agent": "cline"}}),
        ({"harness": {"agent": "none"}}, {"harness": {"agent": "none"}}),
        # The command is never settable from the socket — only the choice.
        ({"harness": {"agent": "cline", "command": ["calc.exe"]}}, {"harness": {"agent": "cline"}}),
        ({"harness": {"agent": "something-else"}}, {}),
        ({"harness": "cline"}, {}),
        ({}, {}),
    ],
)
def test_sanitize_harness(incoming: dict, expected: dict):
    assert sanitize_harness(incoming) == expected


# --- Look deeper: read-only research ---------------------------------------


def _research(repo: Path, scenario: str) -> harness_service.ResearchResult:
    return run_agent_research(
        _config(scenario),
        str(repo),
        "sample.py",
        "@@ -1,2 +1,2 @@",
        "-    return 'hi'\n+    return 'hello'",
        "Why was this changed?",
        threading.Event(),
    )


def _porcelain(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout


def test_look_deeper_reads_history_in_a_copy_and_sees_the_change(repo: Path):
    """The copy carries history (a local clone) and its index is reset to HEAD,
    so `git diff` there shows the change under review. The fake only proceeds
    in plan mode and starts in act, as real Cline does — so this also proves
    the switch to read-only mode happened."""
    (repo / "sample.py").write_bytes(b"def hello():\n    return 'hello'\n")
    before = _porcelain(repo)

    result = _research(repo, "research_git")

    assert "read=yes" in result.answer and "git=yes" in result.answer
    assert "last=init" in result.answer, "history reached the copy"
    assert "changed=sample.py" in result.answer, "the uncommitted change is visible there"
    assert "Let me look" not in result.answer, "narration before '## Answer' is dropped"
    assert result.refused == 0
    assert _porcelain(repo) == before, "the reviewer's repository is untouched"


def test_look_deeper_refuses_every_way_of_writing(repo: Path):
    result = _research(repo, "research_forbidden")
    for refused in ("edit=no", "redirect=no", "other_program=no", "fetch=no", "fs_write=error"):
        assert refused in result.answer
    assert result.refused == 4
    assert (repo / "sample.py").read_bytes() == SAMPLE.encode()


def test_look_deeper_carries_on_when_the_agent_asks_for_input(repo: Path):
    """Measured with real Cline: after a refused command it twice ended its
    turn with "could you confirm...?" — which, behind a button, nobody answers."""
    assert _research(repo, "research_asks").answer == "carried on without asking"


def test_look_deeper_continues_at_most_once(repo: Path):
    answer = _research(repo, "research_asks_twice").answer
    assert "prompt 2" in answer and "prompt 3" not in answer


@pytest.mark.parametrize(
    "command,allowed",
    [
        # What Cline actually sent while this was measured — must pass.
        ("git log -3 --stat -- app/server.py", True),
        ("git -C . log -3 --stat -- a.py; git -C . log -1 -p -- a.py", True),
        ("git show 95b658a -- app/server.py | head -100; git status --porcelain", True),
        ("git log --oneline | head -n 20", True),
        ("git -C {root} status", True),
        ('git -C "{root}" diff HEAD', True),
        ("git --no-pager diff -- app/handlers/narration.py", True),
        ("git rev-parse --show-toplevel", True),
        ("git grep -n review_ended -- app/handlers", True),
        # Must stay refused.
        ("git log > out.txt", False),
        ("git diff --no-index a.txt C:/secret.txt", False),
        ("git show HEAD --output=x.patch", False),
        ("git log; rm -rf .", False),
        ("git log | powershell -c evil", False),
        ("git log && curl http://x", False),
        ("git -C {root}/.. log", False),
        ("git log $(whoami)", False),
        ("git log & del x", False),
        ("cat app/server.py", False),
        ("head -n 5 /etc/passwd", False),
        ("git log | head /etc/passwd", False),
        ("git config --global x y", False),
        ("git -c core.pager=evil log", False),
        ("git blame --contents C:/secret.txt a.py", False),
        ("git grep -Oevil x", False),
        ("git diff --ext-diff", False),
        ("", False),
    ],
)
def test_read_only_command_filter(tmp_path: Path, command: str, allowed: bool):
    assert is_read_only_command(command.replace("{root}", str(tmp_path)), tmp_path) is allowed


# --- The reviewer's own Cline setup ------------------------------------------


def _cline_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data) -> None:
    path = tmp_path / "providers.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("CLINE_PROVIDER_SETTINGS_PATH", str(path))


def test_model_comes_from_the_reviewers_cline_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """ACP sessions don't read these themselves (measured: with no CLINE_*
    variables a session refuses to start), so the app passes all three."""
    _cline_settings(
        tmp_path,
        monkeypatch,
        {
            "lastUsedProvider": "anthropic",
            "providers": {
                "anthropic": {"settings": {"provider": "anthropic", "model": "claude-sonnet-5", "apiKey": "sk-test"}}
            },
        },
    )
    env, model = resolve_agent_env({"agent": "cline"})
    assert (env["CLINE_PROVIDER"], env["CLINE_MODEL"], env["CLINE_API_KEY"]) == (
        "anthropic",
        "claude-sonnet-5",
        "sk-test",
    )
    assert model == AgentModel("anthropic", "claude-sonnet-5", "Cline settings", True)


def test_a_local_provider_gets_a_placeholder_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Measured: Cline refuses any session without CLINE_API_KEY, even Ollama's."""
    _cline_settings(
        tmp_path,
        monkeypatch,
        {
            "lastUsedProvider": "ollama",
            "providers": {"ollama": {"settings": {"provider": "ollama", "model": "qwen3:8b"}}},
        },
    )
    env, model = resolve_agent_env({"agent": "cline"})
    assert env["CLINE_MODEL"] == "qwen3:8b" and env["CLINE_API_KEY"]
    assert model.has_key


def test_a_config_pin_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _cline_settings(
        tmp_path,
        monkeypatch,
        {
            "lastUsedProvider": "anthropic",
            "providers": {"anthropic": {"settings": {"model": "claude-sonnet-5", "apiKey": "sk"}}},
        },
    )
    config = {"agent": "cline", "cline": {"env": {"CLINE_PROVIDER": "ollama", "CLINE_MODEL": "qwen3:8b"}}}
    env, model = resolve_agent_env(config)
    assert (model.provider, model.model, model.source) == ("ollama", "qwen3:8b", "config.yaml")
    assert env["CLINE_API_KEY"] != "sk", "the anthropic key must not be handed to a different provider"


def test_a_sign_in_token_is_preferred_like_cline_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _cline_settings(
        tmp_path,
        monkeypatch,
        {
            "lastUsedProvider": "cline",
            "providers": {
                "cline": {"settings": {"apiKey": "old", "auth": {"accessToken": "token", "apiKey": "nested"}}}
            },
        },
    )
    assert resolve_agent_env({"agent": "cline"})[0]["CLINE_API_KEY"] == "token"


@pytest.mark.parametrize(
    "content", ["", "not json", "[]", '{"providers": "x"}', '{"lastUsedProvider": 3, "providers": {}}']
)
def test_unreadable_cline_settings_mean_not_set_up_never_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
):
    _cline_settings(tmp_path, monkeypatch, content)
    assert resolve_agent_env({"agent": "cline"})[1] == AgentModel(None, None, "none", False)


def test_status_names_the_fix_when_cline_is_not_set_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Checked before a review rather than surfacing as a failed session mid-way."""
    monkeypatch.setattr(harness_service, "resolve_command", lambda command: ["cline"])
    status = harness_status({"agent": "cline"})
    assert not status.available and "cline auth" in status.detail

    _cline_settings(
        tmp_path,
        monkeypatch,
        {
            "lastUsedProvider": "anthropic",
            "providers": {"anthropic": {"settings": {"model": "claude-sonnet-5"}}},
        },
    )
    status = harness_status({"agent": "cline"})
    assert not status.available and "no API key saved for anthropic" in status.detail

    _cline_settings(
        tmp_path,
        monkeypatch,
        {
            "lastUsedProvider": "anthropic",
            "providers": {"anthropic": {"settings": {"model": "claude-sonnet-5", "apiKey": "sk"}}},
        },
    )
    assert harness_status({"agent": "cline"}).available
