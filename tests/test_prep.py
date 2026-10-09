"""Whether the briefings still describe the change (app/handlers/prep.py,
briefing_service.briefing_states)."""

from __future__ import annotations

import json
from pathlib import Path

from app.handlers import prep
from app.services.briefing_service import (
    _hunk_content_hash,
    _pregenerated_briefing_path,
    briefing_states,
)
from app.services.diff_service import Hunk
from app.web.session import Session


def _hunk(index: int, file_path: str = "billing.py", body: str = "+x") -> Hunk:
    header = f"@@ -{index + 1},1 +{index + 1},1 @@"
    return Hunk(index=index, file_path=file_path, header=header, lines=[header, body])


def _brief(repo: Path, hunk: Hunk, *, source: str = "prep-review-skill", content_hash: str | None = None) -> None:
    path = _pregenerated_briefing_path(str(repo), hunk)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "file_path": hunk.file_path,
                "header": hunk.header,
                "content_hash": content_hash or _hunk_content_hash(hunk.diff_context),
                "intent": "Rejects negative amounts.",
                "confidence": "high",
                "source": source,
            }
        ),
        encoding="utf-8",
    )


# --- briefing_states --------------------------------------------------------


def test_states_tell_current_changed_and_missing_apart(tmp_path):
    current, changed, missing = _hunk(0), _hunk(1, "flow.py"), _hunk(2, "other.py")
    _brief(tmp_path, current)
    _brief(tmp_path, changed, content_hash="0" * 64)  # written for code that has since changed
    assert briefing_states(str(tmp_path), [current, changed, missing]) == ["current", "changed", "missing"]


def test_the_apps_own_quick_briefing_does_not_count(tmp_path):
    hunk = _hunk(0)
    _brief(tmp_path, hunk, source="generated")
    assert briefing_states(str(tmp_path), [hunk]) == ["missing"]


def test_a_briefing_orphaned_by_a_shifted_header_reads_as_changed(tmp_path):
    """Editing a line above a hunk moves its header, so its briefing lands
    under another file name — still evidence the hunk was briefed before."""
    old = _hunk(0)
    _brief(tmp_path, old)
    moved = Hunk(index=0, file_path="billing.py", header="@@ -9,1 +9,1 @@", lines=["@@ -9,1 +9,1 @@", "+y"])
    assert briefing_states(str(tmp_path), [moved]) == ["changed"]


# --- prep_status ------------------------------------------------------------


def test_status_counts_and_says_what_is_out_of_date(tmp_path):
    hunks = [_hunk(0), _hunk(1, "flow.py"), _hunk(2, "other.py")]
    _brief(tmp_path, hunks[0])
    _brief(tmp_path, hunks[1], content_hash="0" * 64)
    status = prep.prep_status(Session(hunks, None, str(tmp_path)))
    assert status["out_of_date"] == [1, 2] and status["changed"] == 1 and status["total"] == 3
    assert "2 of 3 changes have no up-to-date briefing (1 changed after they were briefed)" in status["message"]
    assert "use the prep-review skill" in status["message"]


def test_a_pull_request_review_counts_no_change_as_unbriefed(tmp_path, monkeypatch):
    """A PR's why is its own text; nobody is expected to brief its hunks."""
    monkeypatch.setitem(prep.CONFIG["server"], "base_sha", "a" * 40)
    status = prep.prep_status(Session([_hunk(0), _hunk(1, "flow.py")], None, str(tmp_path)))
    assert status["out_of_date"] == [] and status["message"] is None


def test_nothing_to_say_when_everything_is_briefed(tmp_path):
    hunk = _hunk(0)
    _brief(tmp_path, hunk)
    assert prep.prep_status(Session([hunk], None, str(tmp_path)))["message"] is None


def test_repo_level_prep_written_at_an_earlier_commit_is_named(tmp_path, monkeypatch):
    """Stale prep is fed to the model as background fact. Measured on this
    repo: a call map written before a refactor named a module that had been
    deleted, and knew nothing of the package that replaced it."""
    hunk = _hunk(0)
    _brief(tmp_path, hunk)
    monkeypatch.setattr(
        prep,
        "context_status",
        lambda repo: {
            "project_overview": {"present": True, "head_moved": True, "refresh_hint": "use the project-overview skill"},
            "changeset": {"present": False, "refresh_hint": "x"},  # missing is not stale
        },
    )
    status = prep.prep_status(Session([hunk], None, str(tmp_path)))
    assert status["stale_context"] == ["project overview"]
    assert "project overview predate the current commit" in status["message"]
    assert "use the project-overview skill" in status["message"]
