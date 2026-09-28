"""Explaining hunks on request (app/handlers/narration.py): with automatic
explanations off, moving to a hunk shows it without a model call, and
"explain_hunk" narrates it when the reviewer asks."""

from __future__ import annotations

import asyncio

from app.handlers import narration

# Reused scaffolding: the fake socket/model, and the autouse fixture that
# fails any test which would generate a briefing (sends the whole diff).
from tests.test_oversized_hunk import FakeSocket, _briefing, _hunk, _session, no_briefing_generation  # noqa: F401


def _small_session(tmp_path, *, auto: bool):
    session, conversation = _session(tmp_path, [_hunk(3)], _briefing())
    session.auto_narrate = auto
    return session, conversation


def test_auto_on_narrates_on_arrival_as_before(tmp_path):
    session, conversation = _small_session(tmp_path, auto=True)
    ws = FakeSocket()
    asyncio.run(narration.present_current_hunk(ws, session))

    assert conversation.calls == ["present_hunk"]
    assert ws.payload("presenting")["narrating"] is True
    assert "narration" in ws.types()


def test_auto_off_shows_the_hunk_without_a_model_call(tmp_path):
    session, conversation = _small_session(tmp_path, auto=False)
    ws = FakeSocket()
    asyncio.run(narration.present_current_hunk(ws, session))

    assert conversation.calls == []
    assert "narration" not in ws.types()
    presenting = ws.payload("presenting")
    assert presenting["narrating"] is False and presenting["narrated"] is False


def test_explain_hunk_narrates_it_on_request(tmp_path):
    session, conversation = _small_session(tmp_path, auto=False)
    ws = FakeSocket()
    asyncio.run(narration.handle_explain_hunk(ws, session, {"index": 0}))

    assert conversation.calls == ["present_hunk"]
    assert ws.payload("narration")["text"] == "narrated"
    assert "presenting" not in ws.types()  # the code view isn't redrawn


def test_an_explained_hunk_comes_back_from_cache_when_revisited(tmp_path):
    session, conversation = _small_session(tmp_path, auto=False)
    asyncio.run(narration.handle_explain_hunk(FakeSocket(), session, {"index": 0}))
    ws = FakeSocket()
    asyncio.run(narration.present_current_hunk(ws, session))

    assert conversation.calls == ["present_hunk"]  # no second call
    assert ws.payload("presenting")["narrated"] is True
    assert ws.payload("narration")["text"] == "narrated"


def test_a_request_for_a_hunk_no_longer_on_screen_is_dropped(tmp_path):
    session, conversation = _small_session(tmp_path, auto=False)
    ws = FakeSocket()
    asyncio.run(narration.handle_explain_hunk(ws, session, {"index": 5}))
    assert conversation.calls == [] and ws.sent == []


def test_explain_before_the_review_starts_is_refused(tmp_path):
    session, conversation = _small_session(tmp_path, auto=False)
    session.review_started = False
    ws = FakeSocket()
    asyncio.run(narration.handle_explain_hunk(ws, session, {"index": 0}))
    assert conversation.calls == [] and ws.types() == ["error"]


def test_the_preference_message_updates_the_session(tmp_path):
    session, _ = _small_session(tmp_path, auto=True)
    asyncio.run(narration.handle_set_narration_prefs(FakeSocket(), session, {"auto_narrate": False}))
    assert session.auto_narrate is False
    asyncio.run(narration.handle_set_narration_prefs(FakeSocket(), session, {"auto_narrate": "no"}))
    assert session.auto_narrate is False  # not a bool: ignored
