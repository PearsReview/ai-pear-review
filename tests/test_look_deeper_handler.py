"""The look_deeper handler (app/handlers/research.py): guards, what reaches the
client, and how the answer carries into the hunk's conversation. The agent
itself is replaced here — run_agent_research is covered in
tests/test_harness_service.py against a scripted ACP agent."""

from __future__ import annotations

import asyncio

import pytest

from app.handlers import research
from app.services.diff_service import Hunk
from app.services.harness_service import AgentModel, HarnessError, HarnessStatus, ResearchResult
from app.web.session import Session

HUNK = Hunk(index=0, file_path="billing.py", header="@@ -1,1 +1,1 @@", lines=["@@ -1,1 +1,1 @@", "-a", "+b"])
SONNET = AgentModel("anthropic", "claude-sonnet-5", "Cline settings", True)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


@pytest.fixture
def spoken(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What the handler asked to have read aloud — recorded rather than sent
    to a real TTS service, which a unit test must never reach."""
    said: list[str] = []

    async def fake_try_speak(ws, session, text, *args, **kwargs):
        said.append(text)

    monkeypatch.setattr(research, "try_speak", fake_try_speak)
    return said


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, spoken):
    """Records what the handler asked for; answers as a tested model."""
    calls: list[dict] = []

    def fake_research(harness_config, repo_path, file_path, header, diff, question, cancel):
        calls.append({"file_path": file_path, "question": question})
        return ResearchResult("It is read by checkout().", SONNET, tool_calls=3, refused=0)

    monkeypatch.setattr(research, "run_agent_research", fake_research)
    monkeypatch.setattr(research, "harness_status", lambda config: HarnessStatus("cline", True, "ok"))
    return calls


def _look(session: Session, payload: dict) -> FakeSocket:
    ws = FakeSocket()
    asyncio.run(research.handle_look_deeper(ws, session, payload))
    return ws


def test_answer_reaches_the_client_with_the_model_it_ran_on(tmp_path, agent):
    """No per-answer model warning: this is a bring-your-own-model app, so
    that guidance lives in the settings panel, where the model is chosen."""
    session = Session([HUNK], None, str(tmp_path))
    ws = _look(session, {"index": 0, "question": "Who reads this?"})

    [message] = ws.sent
    assert message["type"] == "deeper_turn"
    payload = message["payload"]
    assert payload["text"] == "It is read by checkout()."
    assert payload["model"] == "claude-sonnet-5"
    assert "disclaimer" not in payload and "tested" not in payload
    assert agent == [{"file_path": "billing.py", "question": "Who reads this?"}]


def test_answer_is_read_aloud_and_carries_its_spoken_text(tmp_path, agent, spoken):
    """Like narration: spoken when voice output is on (try_speak decides),
    and "spoken" rides along for the turn's speaker button."""
    ws = _look(Session([HUNK], None, str(tmp_path)), {"index": 0, "question": "Who reads this?"})
    assert ws.sent[0]["payload"]["spoken"] == "It is read by checkout()."
    assert spoken == ["It is read by checkout()."]


def test_answer_carries_into_the_hunks_conversation(tmp_path, agent):
    """So a later normal reply on the hunk can build on what was found."""
    session = Session([HUNK], None, str(tmp_path))
    session.conversation_histories[0] = [
        {"role": "user", "content": "narrate"},
        {"role": "assistant", "content": "narration"},
    ]
    _look(session, {"index": 0, "question": "Who reads this?"})

    history = session.conversation_histories[0]
    assert len(history) == 4
    assert history[2]["role"] == "user" and "Who reads this?" in history[2]["content"]
    assert history[3] == {"role": "assistant", "content": "It is read by checkout()."}


def test_an_empty_history_is_not_seeded(tmp_path, agent):
    """An empty history is how the next reply knows to prepend the diff;
    seeding it would strip that grounding from the follow-up."""
    session = Session([HUNK], None, str(tmp_path))
    _look(session, {"index": 0, "question": "Who reads this?"})
    assert 0 not in session.conversation_histories


def test_a_narration_gets_the_default_question(tmp_path, agent):
    _look(Session([HUNK], None, str(tmp_path)), {"index": 0})
    assert "Look deeper at this change" in agent[0]["question"]


@pytest.mark.parametrize("payload", [{"index": 5}, {"index": -1}, {"index": "0"}, {"index": True}, {}])
def test_an_unknown_hunk_is_an_error_not_a_crash(tmp_path, agent, payload):
    ws = _look(Session([HUNK], None, str(tmp_path)), payload)
    assert [m["type"] for m in ws.sent] == ["error"]
    assert agent == []


def test_an_ended_review_is_still_answered(tmp_path, agent):
    """Ending a review freezes its marks and comments, not the conversation."""
    session = Session([HUNK], None, str(tmp_path))
    session.review_ended = True
    ws = _look(session, {"index": 0})
    assert ws.sent[0]["type"] == "deeper_turn" and len(agent) == 1


def test_an_unavailable_agent_explains_why(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "harness_status", lambda config: HarnessStatus("cline", False, "run cline auth"))
    ws = _look(Session([HUNK], None, str(tmp_path)), {"index": 0})
    assert ws.sent == [{"type": "error", "payload": {"message": "run cline auth"}}]


def test_an_agent_failure_is_reported(tmp_path, monkeypatch):
    def failing(*args):
        raise HarnessError("Cline failed: boom")

    monkeypatch.setattr(research, "run_agent_research", failing)
    monkeypatch.setattr(research, "harness_status", lambda config: HarnessStatus("cline", True, "ok"))
    ws = _look(Session([HUNK], None, str(tmp_path)), {"index": 0})
    assert ws.sent[0]["type"] == "error" and "boom" in ws.sent[0]["payload"]["message"]


def test_an_unexpected_crash_is_still_reported(tmp_path, monkeypatch):
    """A background task's uncaught exception is swallowed by asyncio, which
    would leave the UI's "Looking deeper" spinner running forever."""

    def crashing(*args):
        raise OSError("agent binary vanished")

    monkeypatch.setattr(research, "run_agent_research", crashing)
    monkeypatch.setattr(research, "harness_status", lambda config: HarnessStatus("cline", True, "ok"))
    ws = _look(Session([HUNK], None, str(tmp_path)), {"index": 0})
    assert ws.sent[0]["type"] == "error" and "vanished" in ws.sent[0]["payload"]["message"]
