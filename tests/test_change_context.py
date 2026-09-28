"""Resolving a briefing's related hunks against the live diff."""

from __future__ import annotations

from app.services.briefing_service import Briefing
from app.services.diff_service import Hunk
from app.web.context import change_context
from app.web.session import Session


def _hunk(index: int, file_path: str, header: str) -> Hunk:
    return Hunk(index=index, file_path=file_path, header=header, lines=[header, "+x"])


def _briefing(related, **overrides) -> Briefing:
    data = {
        "intent": "why",
        "alternatives_considered": None,
        "risk_notes": None,
        "confidence": "high",
        "source": "prep-review-skill",
        "related": tuple(related),
    }
    data.update(overrides)
    return Briefing(**data)


HUNKS = [_hunk(0, "a.py", "@@ -1 +1 @@"), _hunk(1, "b.py", "@@ -1 +1 @@"), _hunk(2, "b.py", "@@ -40 +40 @@")]


def test_exact_header_wins_then_first_hunk_in_file_then_dropped(tmp_path):
    session = Session(HUNKS, None, str(tmp_path))
    related = [
        {"file_path": "b.py", "header": "@@ -40 +40 @@", "relation": "caller_of", "note": None},
        {"file_path": "b.py", "header": "@@ -99 +99 @@", "relation": "test_for", "note": None},
        {"file_path": "gone.py", "header": None, "relation": "moved_to", "note": None},
    ]
    summaries = {2: "second b hunk"}
    context = change_context(
        session,
        _briefing(related),
        lambda hunk: _briefing([], summary=summaries.get(hunk.index)) if hunk.index in summaries else None,
    )
    assert [(r.index, r.relation, r.summary) for r in context.related] == [
        (2, "caller_of", "second b hunk"),
        (1, "test_for", None),
    ]


def test_unusable_briefing_or_nothing_resolvable_is_none(tmp_path):
    session = Session(HUNKS, None, str(tmp_path))
    gone = [{"file_path": "gone.py", "header": None, "relation": "moved_to", "note": None}]
    assert change_context(session, _briefing(gone), lambda h: None) is None
    ok = [{"file_path": "b.py", "header": None, "relation": "moved_to", "note": None}]
    assert change_context(session, _briefing(ok, confidence="low"), lambda h: None) is None
