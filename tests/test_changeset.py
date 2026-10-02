"""Change-set themes (.context/changeset.json) as the app reads them."""

from __future__ import annotations

import json

from app.services import changeset


def _write(tmp_path, data) -> None:
    (tmp_path / ".context").mkdir(exist_ok=True)
    (tmp_path / ".context" / "changeset.json").write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
    )


def test_missing_or_unreadable_is_none(tmp_path):
    assert changeset.load_changeset(str(tmp_path)) is None
    _write(tmp_path, "{not json")
    assert changeset.load_changeset(str(tmp_path)) is None
    assert changeset.theme(str(tmp_path), "anything") is None
    assert changeset.changeset_status(str(tmp_path), "abc") == {"present": False}


def test_theme_lookup_caps_text(tmp_path):
    _write(
        tmp_path,
        {
            "base_sha": "abc",
            "themes": [
                {"id": "split", "title": "T" * 200, "why": "W" * 1000, "source": "author-session"},
            ],
        },
    )
    found = changeset.theme(str(tmp_path), "split")
    assert len(found["title"]) == changeset.MAX_TITLE_CHARS
    assert len(found["why"]) == changeset.MAX_WHY_CHARS
    assert changeset.theme(str(tmp_path), "other") is None
    assert changeset.theme(str(tmp_path), None) is None


def test_a_theme_with_no_why_is_not_offered(tmp_path):
    _write(tmp_path, {"themes": [{"id": "empty", "title": "Nothing said"}]})
    assert changeset.theme(str(tmp_path), "empty") is None


def test_status_reports_a_moved_head_without_discarding(tmp_path):
    _write(tmp_path, {"base_sha": "old", "generated_at": "2026-09-14T10:00:00Z", "themes": [{"id": "a", "why": "w"}]})
    status = changeset.changeset_status(str(tmp_path), "new")
    assert status["present"] and status["head_moved"] and status["theme_count"] == 1
    assert changeset.theme(str(tmp_path), "a") is not None
