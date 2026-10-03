"""explore_reply's optional marked_lines (app/handlers/explore.py): the VS Code
extension sends the editor selection; the model sees it, the transcript doesn't."""

from __future__ import annotations

import asyncio
import threading

from app.handlers import dispatch
from app.web import runtime
from app.web.session import Session


class FakeConversation:
    num_ctx = None  # no window to budget against
    total_input_tokens = 0
    total_output_tokens = 0

    def __init__(self) -> None:
        self.questions: list[str] = []

    def arm_cancel(self) -> threading.Event:
        return threading.Event()

    def answer_about_file(self, history, file_path, content, question, from_voice):
        self.questions.append(question)
        return history + [{"role": "user", "content": question}], "It returns the sum."


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    def payloads(self, msg_type: str) -> list[dict]:
        return [m["payload"] for m in self.sent if m["type"] == msg_type]


def _ask(tmp_path, monkeypatch, payload: dict) -> tuple[FakeConversation, FakeSocket]:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    monkeypatch.setattr(runtime.TTS, "synthesize", lambda text: b"audio")
    conversation = FakeConversation()
    session = Session([], conversation, str(tmp_path))  # type: ignore[arg-type]
    ws = FakeSocket()

    async def run():
        await dispatch(ws, session, "explore_reply", payload)
        await session.current_task

    asyncio.run(run())
    return conversation, ws


def test_marked_lines_reach_the_model_but_not_the_transcript(tmp_path, monkeypatch):
    marked = [{"file_path": "calc.py", "old_lineno": 2, "new_lineno": 2, "text": "    return a + b", "kind": "context"}]
    conversation, ws = _ask(
        tmp_path, monkeypatch, {"text": "What does this return?", "file_path": "calc.py", "marked_lines": marked}
    )
    assert "line 2 of calc.py" in conversation.questions[0]
    assert conversation.questions[0].endswith("What does this return?")
    assert ws.payloads("human_turn")[0]["text"] == "What does this return?"
    assert ws.payloads("reviewer_turn")[0]["index"] == -1


def test_without_marked_lines_the_question_is_unchanged(tmp_path, monkeypatch):
    conversation, _ = _ask(tmp_path, monkeypatch, {"text": "What does this file do?", "file_path": "calc.py"})
    assert conversation.questions == ["What does this file do?"]
