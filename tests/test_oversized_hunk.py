"""Speaking about a hunk without sending its diff.

Two triggers. A hunk whose diff can't fit the model's context window
("too_large"): sending it anyway makes Ollama silently drop the front of the
prompt and, on CPU, can outlast timeout_seconds before answering at all. And
a normal call that fails ("call_failed"): retried once from the briefing,
which is a fraction of the size. Either way the answer comes from the cached
briefing and its change context, and a hand-off always follows.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.handlers import narration
from app.services.briefing_service import Briefing, ChangeContext, RelatedHunk
from app.services.conversation_service import ConversationClient, ConversationError
from app.services.diff_service import Hunk
from app.web.context import exceeds_budget
from app.web.session import Session

INTENT = "Pure move: every function went to app/handlers/ or app/web/."
SUMMARY = "Removes the handlers and plumbing from server.py."


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]

    def payload(self, msg_type: str) -> dict:
        return next(m["payload"] for m in self.sent if m["type"] == msg_type)


class FakeConversation:
    """Just enough ConversationClient for the handlers: a small window,
    recorded calls, optional failures, and the real briefing-only prompt."""

    num_ctx = 1024
    max_tokens = 100
    total_input_tokens = 0
    total_output_tokens = 0
    prompts = SimpleNamespace(persona_system="You are a junior engineer.")
    briefing_only_prompt = ConversationClient.briefing_only_prompt

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self.fail = set(fail)
        self.contexts: list[ChangeContext | None] = []

    def arm_cancel(self) -> threading.Event:
        return threading.Event()

    def _wrap_if_from_voice(self, text, from_voice, topic):
        return text

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if name.split(":")[0] in self.fail:
            raise ConversationError("Ollama call failed: Read timed out.")

    def present_hunk(self, history, hunk, briefing, project_context=None, change_context=None):
        self.contexts.append(change_context)
        self._record("present_hunk")
        return history + [
            {"role": "user", "content": hunk.diff_context},
            {"role": "assistant", "content": "n"},
        ], "narrated"

    def present_from_briefing(self, hunk, briefing, change_context=None, reason="too_large"):
        self._record(f"present_from_briefing:{reason}")
        return [{"role": "user", "content": "briefing only"}, {"role": "assistant", "content": "b"}], "from briefing"

    def respond_to_reviewer(self, history, text, from_voice=False):
        self._record("respond_to_reviewer")
        return history + [{"role": "user", "content": text}, {"role": "assistant", "content": "r"}], "normal reply"

    def answer_from_briefing(self, hunk, briefing, question, change_context=None, reason="too_large", from_voice=False):
        self._record(f"answer_from_briefing:{reason}")
        return [{"role": "user", "content": question}, {"role": "assistant", "content": "a"}], "answer from briefing"


def _hunk(n_lines: int, file_path: str = "app/server.py", index: int = 0) -> Hunk:
    body = [f"-    old_line_{i} = compute_something_long(argument_{i})" for i in range(n_lines)]
    header = f"@@ -408,{n_lines} +294,0 @@"
    return Hunk(index=index, file_path=file_path, header=header, lines=[header, *body])


def _briefing(**overrides) -> Briefing:
    data = {
        "intent": INTENT,
        "alternatives_considered": None,
        "risk_notes": None,
        "confidence": "high",
        "source": "prep-review-skill",
        "summary": SUMMARY,
    }
    data.update(overrides)
    return Briefing(**data)


def _session(tmp_path, hunks, briefing: Briefing | None, conversation=None) -> tuple[Session, FakeConversation]:
    conversation = FakeConversation() if conversation is None else conversation
    session = Session(hunks, conversation, str(tmp_path))
    session.index = 0
    session.review_started = True
    session.tts_enabled = False
    if briefing is not None:
        session.briefings[0] = briefing
    return session, conversation


@pytest.fixture(autouse=True)
def no_briefing_generation(monkeypatch):
    """Generating a briefing sends the whole diff — exactly what these paths
    must never do — so any attempt fails the test."""

    def refuse(*args, **kwargs):
        raise AssertionError("briefing generation must not run here")

    monkeypatch.setattr(narration.BRIEFING, "analyze_hunk", refuse)
    monkeypatch.setattr(narration.BRIEFING, "load_cached", lambda hunk: None)


# --- prompt -----------------------------------------------------------------


def _client() -> ConversationClient:
    return ConversationClient.__new__(ConversationClient)


def test_briefing_only_prompt_leaves_out_the_diff_and_says_so():
    prompt = _client().briefing_only_prompt(_hunk(50), _briefing(), None, "Where did the code go?", "too_large")
    assert "old_line_3" not in prompt
    assert "too large" in prompt and "NOT included" in prompt
    assert INTENT in prompt and SUMMARY in prompt
    assert "Where did the code go?" in prompt
    assert "0 lines added, 50 removed" in prompt


def test_call_failed_wording_does_not_claim_the_hunk_was_too_large():
    prompt = _client().briefing_only_prompt(_hunk(5), _briefing(), None, None, "call_failed")
    assert "too large" not in prompt
    assert "NOT included" in prompt and "old_line_1" not in prompt


def test_briefing_only_prompt_carries_theme_and_related_summaries():
    context = ChangeContext(
        "Split server.py",
        "server.py was 2660 lines.",
        (RelatedHunk(1, "app/handlers/narration.py", "moved_to", "handle_reply", "Adds the narration handlers."),),
    )
    prompt = _client().briefing_only_prompt(_hunk(5), _briefing(), context, "q", "too_large")
    assert "server.py was 2660 lines." in prompt
    assert "moved to app/handlers/narration.py (handle_reply): Adds the narration handlers." in prompt


def test_briefing_only_prompt_keeps_the_briefing_trust_rules():
    """A low-confidence briefing adds nothing, summary included, and a
    locally generated risk note never gets in."""
    low = _client().briefing_only_prompt(_hunk(5), _briefing(confidence="low"), None, "q", "too_large")
    assert INTENT not in low and SUMMARY not in low
    generated = _briefing(source="generated", risk_notes="max() could have a higher time complexity")
    assert "time complexity" not in _client().briefing_only_prompt(_hunk(5), generated, None, "q", "too_large")


def test_exceeds_budget():
    assert not exceeds_budget(None, "s", [], "x" * 100_000)  # no window to budget against
    assert exceeds_budget(100, "s", [], "x" * 1_000)
    assert not exceeds_budget(1_000, "s", [], "x" * 1_000)


# --- replies ----------------------------------------------------------------


def test_oversized_reply_answers_from_the_briefing_then_offers_a_handoff(tmp_path):
    session, conversation = _session(tmp_path, [_hunk(400)], _briefing())
    ws = FakeSocket()

    asyncio.run(narration.handle_reply(ws, session, {"text": "Where did the code go?"}))

    assert conversation.calls == ["answer_from_briefing:too_large"]
    assert ws.types().index("reviewer_turn") < ws.types().index("context_too_large")
    notice = ws.payload("context_too_large")
    assert notice["kind"] == "hunk" and notice["reason"] == "too_large" and notice["answered_from_briefing"] is True
    assert "Where did the code go?" in notice["handoff_text"]
    # The stored history has no diff, so the next question fits normally.
    assert all("old_line_" not in m["content"] for m in session.conversation_histories[0])


def test_follow_up_after_a_briefing_answer_takes_the_normal_path(tmp_path):
    session, conversation = _session(tmp_path, [_hunk(400)], _briefing())
    asyncio.run(narration.handle_reply(FakeSocket(), session, {"text": "Where did the code go?"}))

    ws = FakeSocket()
    asyncio.run(narration.handle_reply(ws, session, {"text": "Anything risky?"}))

    assert conversation.calls == ["answer_from_briefing:too_large", "respond_to_reviewer"]
    assert "context_too_large" not in ws.types()


def test_oversized_reply_without_a_briefing_makes_no_model_call(tmp_path):
    session, conversation = _session(tmp_path, [_hunk(400)], briefing=None)
    ws = FakeSocket()

    asyncio.run(narration.handle_reply(ws, session, {"text": "Where did the code go?"}))

    assert conversation.calls == []
    assert "reviewer_turn" not in ws.types()
    assert ws.payload("context_too_large")["answered_from_briefing"] is False


def test_oversized_reply_with_selected_lines_answers_even_without_a_briefing(tmp_path):
    session, conversation = _session(tmp_path, [_hunk(400)], briefing=None)
    ws = FakeSocket()
    marked = [
        {
            "file_path": "app/server.py",
            "old_lineno": 410,
            "new_lineno": None,
            "kind": "del",
            "text": "    old_line_2 = compute_something_long(argument_2)",
        }
    ]

    asyncio.run(narration.handle_reply(ws, session, {"text": "Why this line?", "marked_lines": marked}))

    assert conversation.calls == ["answer_from_briefing:too_large"]
    assert ws.payload("context_too_large")["answered_from_briefing"] is True


def test_a_hunk_that_fits_is_unaffected(tmp_path):
    session, conversation = _session(tmp_path, [_hunk(3)], _briefing())
    ws = FakeSocket()

    asyncio.run(narration.handle_reply(ws, session, {"text": "Why?"}))

    assert conversation.calls == ["respond_to_reviewer"]
    assert "context_too_large" not in ws.types()


def test_a_failed_reply_is_retried_once_from_the_briefing(tmp_path):
    session, conversation = _session(
        tmp_path, [_hunk(3)], _briefing(), conversation=FakeConversation(fail=("respond_to_reviewer",))
    )
    ws = FakeSocket()

    asyncio.run(narration.handle_reply(ws, session, {"text": "Why?"}))

    assert conversation.calls == ["respond_to_reviewer", "answer_from_briefing:call_failed"]
    assert "error" not in ws.types()
    notice = ws.payload("context_too_large")
    assert notice["reason"] == "call_failed" and notice["answered_from_briefing"] is True


def test_a_failed_reply_without_a_briefing_is_an_error_and_no_retry(tmp_path):
    session, conversation = _session(
        tmp_path, [_hunk(3)], briefing=None, conversation=FakeConversation(fail=("respond_to_reviewer",))
    )
    ws = FakeSocket()

    asyncio.run(narration.handle_reply(ws, session, {"text": "Why?"}))

    assert conversation.calls == ["respond_to_reviewer"]
    assert "error" in ws.types() and "context_too_large" not in ws.types()


# --- narration --------------------------------------------------------------


def test_oversized_hunk_is_narrated_from_its_briefing(tmp_path):
    session, conversation = _session(tmp_path, [_hunk(400)], _briefing())
    ws = FakeSocket()

    asyncio.run(narration.present_current_hunk(ws, session))

    assert conversation.calls == ["present_from_briefing:too_large"]
    assert ws.types().index("narration") < ws.types().index("context_too_large")
    assert ws.payload("context_too_large")["answered_from_briefing"] is True


def test_oversized_hunk_without_a_briefing_says_so_without_a_model_call(tmp_path):
    session, conversation = _session(tmp_path, [_hunk(400)], briefing=None)
    ws = FakeSocket()

    asyncio.run(narration.present_current_hunk(ws, session))

    assert conversation.calls == []
    assert "too large" in ws.payload("narration")["text"]
    assert ws.payload("context_too_large")["answered_from_briefing"] is False


def test_a_failed_narration_is_retried_once_from_the_briefing(tmp_path):
    session, conversation = _session(
        tmp_path, [_hunk(3)], _briefing(), conversation=FakeConversation(fail=("present_hunk",))
    )
    ws = FakeSocket()

    asyncio.run(narration.present_current_hunk(ws, session))

    assert conversation.calls == ["present_hunk", "present_from_briefing:call_failed"]
    assert ws.payload("narration")["text"] == "from briefing"
    assert ws.payload("context_too_large")["reason"] == "call_failed"


def test_narration_carries_theme_to_the_model_and_related_links_to_the_client(tmp_path):
    (tmp_path / ".context").mkdir()
    (tmp_path / ".context" / "changeset.json").write_text(
        json.dumps(
            {
                "base_sha": None,
                "themes": [
                    {
                        "id": "server-split",
                        "title": "Split server.py",
                        "why": "It was 2660 lines.",
                        "source": "author-session",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    hunks = [_hunk(3), _hunk(3, file_path="app/handlers/narration.py", index=1)]
    briefing = _briefing(
        kind="move",
        theme="server-split",
        related=(
            {
                "file_path": "app/handlers/narration.py",
                "header": "@@ stale header @@",
                "relation": "moved_to",
                "note": "handle_reply",
            },
            {"file_path": "not/in/the/diff.py", "header": None, "relation": "moved_to", "note": None},
        ),
    )
    session, conversation = _session(tmp_path, hunks, briefing)
    ws = FakeSocket()

    asyncio.run(narration.present_current_hunk(ws, session))

    context = conversation.contexts[0]
    assert context.theme_why == "It was 2660 lines."
    # Header didn't match, so it fell back to the file's first hunk; the
    # file outside the diff was dropped.
    assert ws.payload("narration")["related"] == [
        {"index": 1, "file_path": "app/handlers/narration.py", "relation": "moved_to", "note": "handle_reply"}
    ]


def test_degraded_mode_shows_the_briefing_as_text(tmp_path):
    session, _ = _session(tmp_path, [_hunk(3)], _briefing())
    session.conversation = None
    ws = FakeSocket()

    asyncio.run(narration.present_current_hunk(ws, session))

    text = ws.payload("narration")["text"]
    assert "Narration unavailable" in text and SUMMARY in text and INTENT in text
