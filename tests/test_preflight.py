"""app/services/preflight.py — the checks a first run reports.

These exist because every failure here was already handled *gracefully* and
therefore silently: Ollama not running, the model never pulled, and a clean
working tree all produced the same symptom — a UI that opens and then does
nothing. The value is entirely in saying which one happened, so the tests
care as much about the fix text as about the verdict.

The other property under test is that a preflight can never itself break a
startup it was added to reassure people about.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.services.preflight import Check, format_report, has_fatal, run_checks


def _repo(path: Path, dirty: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "a@b.c"],
        ["config", "user.name", "a"],
        ["add", "-A"],
        ["commit", "-q", "-m", "i"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    if dirty:
        (path / "a.py").write_text("x = 2\n", encoding="utf-8")
    return path


def _config(repo_path: str, **conversation) -> dict:
    base = {"provider": "ollama", "ollama": {"base_url": "http://127.0.0.1:9", "model": "m"}}
    base.update(conversation)
    return {"server": {"repo_path": repo_path}, "conversation": base}


def _named(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name == name)


def test_a_healthy_repo_reports_no_fatal_problems(tmp_path: Path):
    checks = run_checks(_config(str(_repo(tmp_path / "r"))))
    assert not has_fatal(checks)
    assert _named(checks, "repository").ok
    assert _named(checks, "changes").ok


def test_a_path_that_is_not_a_repo_is_fatal_and_says_what_to_do(tmp_path: Path):
    """Fatal because nothing downstream can mean anything — but the message
    has to name both ways out, since "run it from the right place" and
    "point the config somewhere else" are both legitimate."""
    checks = run_checks(_config(str(tmp_path)))
    repository = _named(checks, "repository")
    assert repository.fatal and not repository.ok
    assert has_fatal(checks)
    assert "repo_path" in repository.fix


def test_checks_after_a_fatal_one_are_not_run(tmp_path: Path):
    """No point reporting on a repo's diff when the path isn't a repo. A
    wall of consequential failures buries the one that matters."""
    checks = run_checks(_config(str(tmp_path)))
    assert [c.name for c in checks] == ["git", "repository"]


def test_a_clean_tree_warns_rather_than_blocking(tmp_path: Path):
    """Nothing to review *yet* is not a reason to refuse to start — the
    reviewer may be about to edit something, and Refresh Diff exists."""
    checks = run_checks(_config(str(_repo(tmp_path / "r", dirty=False))))
    changes = _named(checks, "changes")
    assert not changes.ok
    assert not changes.fatal
    assert not has_fatal(checks)


def test_untracked_files_count_as_changes(tmp_path: Path):
    """The app presents each untracked file as a whole-file hunk, so a repo
    whose only changes are new files has plenty to review. Reporting "no
    changes" there would send someone looking for a bug that isn't there."""
    repo = _repo(tmp_path / "r", dirty=False)
    (repo / "brand_new.py").write_text("y = 2\n", encoding="utf-8")
    assert _named(run_checks(_config(str(repo))), "changes").ok


def test_an_unreachable_model_server_warns_with_the_fix(tmp_path: Path):
    checks = run_checks(_config(str(_repo(tmp_path / "r"))))  # port 9 is never listening
    server = _named(checks, "model server")
    assert not server.ok and not server.fatal
    assert "ollama serve" in server.fix
    # The distinction that matters: this is not a model problem, so no
    # advice about pulling one should appear.
    assert not any(c.name == "model" for c in checks)


def test_a_missing_model_is_reported_separately_from_a_missing_server(monkeypatch, tmp_path: Path):
    """Two checks rather than one, because the fixes are entirely
    different — start a server versus pull a model — and conflating them is
    the confusion this module exists to remove."""

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "other-model:latest"}]}

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response())

    checks = run_checks(_config(str(_repo(tmp_path / "r")), ollama={"base_url": "http://x", "model": "wanted:7b"}))
    assert _named(checks, "model server").ok
    model = _named(checks, "model")
    assert not model.ok
    assert "ollama pull wanted:7b" in model.fix
    assert "other-model:latest" in model.fix, "say what IS installed, not just what isn't"


def test_a_model_configured_without_a_tag_still_matches(monkeypatch, tmp_path: Path):
    """Ollama reports "name:tag"; writing just the name in config is a
    normal thing to do and must not read as "not installed"."""

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "qwen2.5-coder:7b-instruct-q4_K_M"}]}

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response())

    checks = run_checks(_config(str(_repo(tmp_path / "r")), ollama={"base_url": "http://x", "model": "qwen2.5-coder"}))
    assert _named(checks, "model").ok


def test_anthropic_reports_the_key_it_actually_looks_for(monkeypatch, tmp_path: Path):
    """The env var name is configurable, so the message has to name the one
    actually being looked for — telling someone to set ANTHROPIC_API_KEY
    when the config reads a different variable is worse than saying nothing."""
    repo = str(_repo(tmp_path / "r"))
    config = _config(repo, provider="anthropic", anthropic={"api_key_env": "MY_KEY"})

    monkeypatch.delenv("MY_KEY", raising=False)
    key = _named(run_checks(config), "api key")
    assert not key.ok
    assert "MY_KEY" in key.detail and "MY_KEY" in key.fix

    monkeypatch.setenv("MY_KEY", "sk-ant-whatever")
    assert _named(run_checks(config), "api key").ok


def test_an_unused_anthropic_key_is_pointed_out_when_ollama_is_down(monkeypatch, tmp_path: Path):
    """The one dead end the preflight used to walk people into.

    A key alone doesn't switch providers — conversation.provider does, and
    it ships as "ollama". Without this the report tells someone who
    deliberately chose the API to go and install a local model server, and
    never mentions the key they set."""
    monkeypatch.setenv("MY_KEY", "sk-ant-whatever")
    config = _config(str(_repo(tmp_path / "r")), anthropic={"api_key_env": "MY_KEY"})

    checks = run_checks(config)  # port 9 is never listening
    hint = _named(checks, "anthropic key")
    assert not hint.ok and not hint.fatal
    assert "MY_KEY" in hint.detail, "name the variable actually being read"
    assert "conversation.provider" in hint.fix


def test_a_working_ollama_setup_says_nothing_about_an_anthropic_key(monkeypatch, tmp_path: Path):
    """A key exported for some other tool is common and none of this app's
    business. It's only worth raising when the configured provider can't
    serve the request and this one could — otherwise it's a WARN on a setup
    that is entirely fine, which is how people learn to ignore the report."""

    class _Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": "m:latest"}]}

    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")

    checks = run_checks(_config(str(_repo(tmp_path / "r")), ollama={"base_url": "http://x", "model": "m"}))
    assert _named(checks, "model server").ok and _named(checks, "model").ok
    assert not any(c.name == "anthropic key" for c in checks)


def test_no_anthropic_hint_when_there_is_no_key_to_suggest(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    checks = run_checks(_config(str(_repo(tmp_path / "r"))))  # ollama unreachable
    assert not _named(checks, "model server").ok
    assert not any(c.name == "anthropic key" for c in checks)


def test_an_unknown_provider_is_reported_rather_than_ignored(tmp_path: Path):
    checks = run_checks(_config(str(_repo(tmp_path / "r")), provider="wat"))
    assert not _named(checks, "provider").ok


def test_the_report_is_plain_ascii(tmp_path: Path):
    """This is the first thing a user sees. A mojibake tick on a Windows
    console reads as the app already being broken."""
    report = format_report(run_checks(_config(str(_repo(tmp_path / "r")))))
    report.encode("ascii")  # raises if anything non-ASCII crept in
    assert "[OK  ]" in report


def test_every_check_appears_in_the_report_with_its_fix(tmp_path: Path):
    checks = run_checks(_config(str(_repo(tmp_path / "r", dirty=False))))
    report = format_report(checks)
    for check in checks:
        assert check.name in report
        if check.fix:
            assert check.fix in report
