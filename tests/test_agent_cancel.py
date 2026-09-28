"""A Look deeper, Act Now or refine run cancelled before it answers tells the
client so ("agent_stopped"). Every cancels=True action — Interrupt, a
speaker button, Explain, Next — cancels the one task in flight, and a run
that ended silently left the UI's spinner running and Act Now's bar locked
with nothing coming to release them."""

from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest

from app.handlers import act_now, dispatch, research
from app.services.diff_service import Hunk
from app.services.harness_service import ActNowRequest, HarnessError, HarnessStatus, ProposedChange
from app.web.session import Session

HUNK = Hunk(index=0, file_path="billing.py", header="@@ -1,1 +1,1 @@", lines=["@@ -1,1 +1,1 @@", "-a", "+b"])


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class ClosedSocket:
    """A browser that has gone: the disconnect is what cancelled the run."""

    async def send_json(self, data: dict) -> None:
        raise RuntimeError("websocket is closed")


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Both agents stand in as runs that last until they are cancelled."""
    event = threading.Event()

    def blocking(*args, **kwargs):
        event.set()
        cancel = args[-1]
        cancel.wait(5)
        raise HarnessError("cancelled")

    monkeypatch.setattr(research, "run_agent_research", blocking)
    monkeypatch.setattr(act_now, "run_agent_edit", blocking)
    available = lambda config: HarnessStatus("cline", True, "ok")  # noqa: E731
    monkeypatch.setattr(research, "harness_status", available)
    monkeypatch.setattr(act_now, "harness_status", available)
    return event


def _start_then_stop(ws, session: Session, started: threading.Event, msg_type: str, payload: dict) -> None:
    async def run() -> None:
        await dispatch(ws, session, msg_type, payload)
        task = session.current_task
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), f"the agent never started: {getattr(ws, 'sent', None)}"
        await dispatch(ws, session, "stop", {})
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()

    asyncio.run(run())


def _stopped(ws: FakeSocket) -> list[dict]:
    return [m["payload"] for m in ws.sent if m["type"] == "agent_stopped"]


def test_a_stopped_look_deeper_is_reported(tmp_path, started):
    ws = FakeSocket()
    _start_then_stop(ws, Session([HUNK], None, str(tmp_path)), started, "look_deeper", {"index": 0})
    [stopped] = _stopped(ws)
    assert stopped["kind"] == "look_deeper"
    assert not [m for m in ws.sent if m["type"] == "error"]


def test_a_stopped_act_now_is_reported(tmp_path, started):
    session = Session([HUNK], None, str(tmp_path))
    session.index = 0
    ws = FakeSocket()
    _start_then_stop(ws, session, started, "act_now", {"text": "Rename it"})
    [stopped] = _stopped(ws)
    assert stopped["kind"] == "act_now"
    assert "nothing was changed" in stopped["message"]


def test_a_stopped_refine_keeps_the_pending_preview(tmp_path, started):
    session = Session([HUNK], None, str(tmp_path))
    pending = (ProposedChange("billing.py", "a\n", "b\n"),)
    session.pending_act_now = pending
    session.act_now_request = ActNowRequest("billing.py", "line 1", "a", ("Rename it",))
    ws = FakeSocket()

    _start_then_stop(ws, session, started, "refine_act_now", {"text": "Shorter name"})

    [stopped] = _stopped(ws)
    assert stopped["kind"] == "refine_act_now"
    assert session.pending_act_now == pending
    assert session.act_now_request is not None


def test_a_closed_socket_still_ends_as_cancelled(tmp_path, started):
    """The report is best-effort; the CancelledError must still propagate."""
    _start_then_stop(ClosedSocket(), Session([HUNK], None, str(tmp_path)), started, "look_deeper", {"index": 0})
