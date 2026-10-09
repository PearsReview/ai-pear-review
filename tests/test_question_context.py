"""Facts fetched for a reply only when its question calls for them
(app/web/question_context.py)."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.handlers import narration
from app.services.code_search import Definition, DefinitionNotFound
from app.services.diff_service import Hunk
from app.web.question_context import question_context, routes_for
from app.web.session import Session

HUNK = Hunk(
    index=0,
    file_path="billing.py",
    header="@@ -8,2 +8,3 @@",
    lines=["@@ -8,2 +8,3 @@", " def charge(amount):", "-    return amount", "+    return validate_amount(amount)"],
)


@pytest.mark.parametrize(
    "question,expected",
    [
        # Callers and tests aren't looked up here any more (see the module
        # docstring); they're due to come from the coding agent.
        ("Is this tested?", []),
        ("Who calls this?", []),
        ("Why was this changed?", ["why"]),
        ("What does `validate_amount` do?", ["definition"]),
        ("is validate_amount tested?", ["definition"]),
        ("I detest this naming", []),
        ("Looks good to me", []),
    ],
)
def test_routing(question, expected):
    assert routes_for(question) == expected


def _write_map(repo: Path, **charge_fields) -> None:
    charge = {"name": "charge", "file": "billing.py", "callers": [{"name": "checkout", "file": "flow.py"}]}
    charge.update(charge_fields)
    (repo / ".context").mkdir(exist_ok=True)
    (repo / ".context" / "call_map.json").write_text(json.dumps({"symbols": [charge]}), encoding="utf-8")


def _session(repo: Path) -> Session:
    return Session([HUNK], None, str(repo))


def _no_history(repo, hunk):
    raise AssertionError("history lookup should not run")


def test_a_call_map_on_disk_is_not_used(tmp_path):
    """The call-map skill's output is switched off for now, even when a
    repo still has one."""
    _write_map(tmp_path, test_caller_count=2, test_files=["tests/test_billing.py"])
    session = _session(tmp_path)
    assert question_context(session, HUNK, "Is this tested?", [], False, history_lookup=_no_history) is None
    assert question_context(session, HUNK, "Who calls this?", [], False, history_lookup=_no_history) is None


def test_why_uses_line_history_only_without_a_briefing(tmp_path):
    session = _session(tmp_path)
    found = question_context(
        session,
        HUNK,
        "why was this changed?",
        [],
        False,
        history_lookup=lambda repo, hunk: ["abc123 2026-09-01 Reject negative charges"],
    )
    assert "Reject negative charges" in found.text
    assert question_context(session, HUNK, "why was this changed?", [], True, history_lookup=_no_history) is None


def test_definitions_only_for_names_the_hunk_mentions(tmp_path):
    looked_up = []

    def lookup(repo, name):
        looked_up.append(name)
        if name == "validate_amount":
            return Definition(
                "checks.py",
                3,
                ["", "", "def validate_amount(amount):", "    if amount < 0:", "        raise ValueError"],
                1,
            )
        raise DefinitionNotFound(name)

    found = question_context(
        _session(tmp_path),
        HUNK,
        "what do `validate_amount` and `unrelated_helper` do?",
        [],
        False,
        definition_lookup=lookup,
    )
    assert looked_up == ["validate_amount"]
    assert "checks.py:3" in found.text and "def validate_amount(amount):" in found.text


def test_blocks_that_would_overflow_the_cap_are_skipped_whole(tmp_path):
    huge = Definition("checks.py", 1, ["x" * 2000], 1)
    found = question_context(
        _session(tmp_path),
        HUNK,
        "what does `validate_amount` do?",
        [],
        False,
        definition_lookup=lambda repo, name: huge,
    )
    assert found is None


def test_nothing_routed_means_nothing_added(tmp_path):
    _write_map(tmp_path, test_caller_count=2, test_files=["tests/test_billing.py"])
    assert question_context(_session(tmp_path), HUNK, "Looks good", [], False) is None


# --- in the reply handler ---------------------------------------------------


class _Conversation:
    num_ctx = 8192
    max_tokens = 100
    total_input_tokens = 0
    total_output_tokens = 0
    prompts = SimpleNamespace(persona_system="You are a junior engineer.")

    def __init__(self):
        self.sent: list[str] = []

    def arm_cancel(self):
        return threading.Event()

    def respond_to_reviewer(self, history, text, from_voice=False):
        self.sent.append(text)
        return history + [{"role": "user", "content": text}, {"role": "assistant", "content": "r"}], "reply"


class _Socket:
    async def send_json(self, data):
        pass


def test_reply_prompt_carries_the_looked_up_facts(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(narration.BRIEFING, "load_cached", lambda hunk: None)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "checks.py").write_text("def validate_amount(amount):\n    return amount\n", encoding="utf-8")
    conversation = _Conversation()
    session = Session([HUNK], conversation, str(tmp_path))
    session.index = 0
    session.tts_enabled = False

    question = "What does `validate_amount` do?"
    asyncio.run(narration.handle_reply(_Socket(), session, {"text": question}))

    sent = conversation.sent[0]
    assert sent.index("Where validate_amount is defined (checks.py:1)") < sent.index(question)
    assert "+    return validate_amount(amount)" in sent  # the diff is still there
    # A cached "unavailable" would stop narration generating a briefing later.
    assert 0 not in session.briefings


def test_looked_up_facts_are_dropped_before_the_diff_when_the_prompt_is_tight(tmp_path, monkeypatch):
    """Optional context goes first: a reply that fits without it must not be
    pushed into the briefing-only fallback by it."""
    monkeypatch.setattr(narration.BRIEFING, "load_cached", lambda hunk: None)
    monkeypatch.setattr(
        narration, "question_context", lambda *a, **k: SimpleNamespace(text="F" * 3000, routes=("tests",))
    )
    conversation = _Conversation()
    conversation.num_ctx = 1024  # budget ~412 tokens: the diff fits, 3000 chars of facts don't
    session = Session([HUNK], conversation, str(tmp_path))
    session.index = 0
    session.tts_enabled = False

    asyncio.run(narration.handle_reply(_Socket(), session, {"text": "Is this tested?"}))

    assert len(conversation.sent) == 1 and "FFFF" not in conversation.sent[0]
    assert "validate_amount" in conversation.sent[0]


def test_line_history_reads_commit_subjects_for_the_changed_lines(tmp_path):
    import subprocess

    from app.services.diff_service import get_review_hunks, line_history

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "billing.py").write_text("def charge(amount):\n    return amount\n", encoding="utf-8")
    git("init", "-q")
    git("config", "user.email", "t@e.st")
    git("config", "user.name", "T")
    git("add", "-A")
    git("commit", "-q", "-m", "Add charge")
    (tmp_path / "billing.py").write_text("def charge(amount):\n    return abs(amount)\n", encoding="utf-8")
    git("commit", "-qam", "Never charge a negative amount")
    (tmp_path / "billing.py").write_text("def charge(amount):\n    return validate(amount)\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("x = 1\n", encoding="utf-8")

    hunks = get_review_hunks(str(tmp_path))
    changed = next(h for h in hunks if h.file_path == "billing.py")
    subjects = line_history(str(tmp_path), changed)
    assert subjects and subjects[0].endswith("Never charge a negative amount")
    assert line_history(str(tmp_path), next(h for h in hunks if h.file_path == "new.py")) == []
