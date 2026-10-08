"""After a review ends (app/handlers/review_flow.py): chat keeps working,
the marks and comments are frozen with a message that says how to get them
back, and "reopen_review" picks the same review up again."""

from __future__ import annotations

import asyncio

from app.handlers import comments, narration, review_flow
from app.services.session_store import load_persisted_state

# Reused scaffolding: the fake socket/model, and the autouse fixture that
# fails any test which would generate a briefing (sends the whole diff).
from tests.test_oversized_hunk import FakeSocket, _briefing, _hunk, _session, no_briefing_generation  # noqa: F401


def _ended_session(tmp_path, *, reviewed: set[int] | None = None):
    session, conversation = _session(tmp_path, [_hunk(3), _hunk(3)], _briefing())
    session.auto_narrate = False
    session.review_started = True
    session.review_ended = True
    session.reviewed = set(reviewed or ())
    return session, conversation


def _run(handler, session, payload=None) -> FakeSocket:
    """Runs a handler, then whatever it left running as session.current_task,
    so a handler that hands off to present_current_hunk is seen to the end."""
    ws = FakeSocket()

    async def go() -> None:
        await handler(ws, session, payload or {})
        if session.current_task is not None:
            await session.current_task

    asyncio.run(go())
    return ws


# --- chat keeps working ------------------------------------------------------


def test_a_question_is_answered_after_the_review_ends(tmp_path):
    session, conversation = _ended_session(tmp_path)
    ws = _run(narration.handle_reply, session, {"text": "Anything risky?"})

    assert conversation.calls == ["respond_to_reviewer"]
    assert "reviewer_turn" in ws.types() and "error" not in ws.types()


def test_explain_on_request_works_after_the_review_ends(tmp_path):
    session, conversation = _ended_session(tmp_path)
    ws = _run(narration.handle_explain_hunk, session, {"index": 0})

    assert conversation.calls == ["present_hunk"]
    assert ws.payload("narration")["text"] == "narrated"


def test_moving_to_a_hunk_after_the_end_still_doesnt_narrate_by_itself(tmp_path):
    session, conversation = _ended_session(tmp_path)
    session.auto_narrate = True
    ws = FakeSocket()
    asyncio.run(narration.present_current_hunk(ws, session))

    assert conversation.calls == []
    assert ws.payload("presenting")["review_ended"] is True


# --- marks and comments are frozen, and say why ------------------------------


def test_marking_after_the_end_says_to_reopen(tmp_path):
    session, _ = _ended_session(tmp_path, reviewed={0})
    for handler in (review_flow.handle_toggle_reviewed, review_flow.handle_toggle_reviewed_all):
        ws = _run(handler, session)
        assert ws.types() == ["error"]
        assert "reopen" in ws.payload("error")["message"]
    assert session.reviewed == {0}  # unchanged


def test_commenting_after_the_end_says_to_reopen(tmp_path):
    session, _ = _ended_session(tmp_path)
    ws = _run(comments.handle_request_change, session, {"text": "Rename this."})

    assert ws.types() == ["error"]
    assert "reopen" in ws.payload("error")["message"]
    assert session.pending_review_comments == []


def test_commenting_before_the_start_still_says_to_start(tmp_path):
    session, _ = _ended_session(tmp_path)
    session.review_started = session.review_ended = False
    ws = _run(comments.handle_request_change, session, {"text": "Rename this."})
    assert ws.payload("error")["message"] == "Start the review before adding comments."


# --- reopening ---------------------------------------------------------------


def test_reopen_keeps_the_marks_and_unlocks_them(tmp_path):
    session, _ = _ended_session(tmp_path, reviewed={0, 1})
    session.index = 1
    ws = _run(review_flow.handle_reopen_review, session)

    assert session.review_ended is False and session.review_started is True
    assert session.reviewed == {0, 1}
    assert "Review reopened" in ws.payload("notice")["message"]
    presenting = ws.payload("presenting")
    assert presenting["index"] == 1 and presenting["review_ended"] is False
    assert load_persisted_state(str(tmp_path)).review_ended is False

    # Unmarking now works, and doesn't end the review again.
    _run(review_flow.handle_toggle_reviewed, session)
    assert session.reviewed == {0} and session.review_ended is False


def test_reopen_from_the_summary_screen_lands_on_the_first_hunk(tmp_path):
    session, _ = _ended_session(tmp_path)
    session.index = -1
    ws = _run(review_flow.handle_reopen_review, session)
    assert ws.payload("presenting")["index"] == 0


def test_marking_the_last_hunk_after_reopening_ends_it_again(tmp_path):
    session, _ = _ended_session(tmp_path, reviewed={0})
    session.index = 1
    _run(review_flow.handle_reopen_review, session)
    _run(review_flow.handle_toggle_reviewed, session)
    assert session.review_ended is True


def test_reopen_when_not_ended_does_nothing(tmp_path):
    session, _ = _ended_session(tmp_path)
    session.review_ended = False
    ws = _run(review_flow.handle_reopen_review, session)
    assert ws.sent == []
