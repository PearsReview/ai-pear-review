"""Finish Review's hand-off (app/services/review_handoff.py and
comments.handle_finish_review): each queued comment keeps a reference to
its code rather than whole files, is checked against the current file when
the review finishes, and the queue becomes a single markdown plan for the
reviewer's coding agent — the structured review it is built from is never
written to disk."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.handlers import comments
from app.services.diff_service import Hunk
from app.services.review_handoff import (
    SKILL_MARKER,
    build_review,
    check_status,
    comment_location,
    latest_review_plan,
    render_plan,
    render_skill,
)
from app.web.session import Session

# app.py as it is now; the diff below turned line 2 of the old file into
# lines 2-4 of this one.
WORKING = [
    "def total(price, qty):",
    "    tax = 0.2",
    "    subtotal = price * qty",
    "    return subtotal * (1 + tax)",
    "",
    "def other():",
    "    pass",
    "",
    "FAR_AWAY_MARKER = 1",
]
FULL_LINES = [
    {"kind": "context", "old_lineno": 1, "new_lineno": 1, "text": WORKING[0]},
    {"kind": "del", "old_lineno": 2, "new_lineno": None, "text": "    return price * qty * 1.2"},
    {"kind": "add", "old_lineno": None, "new_lineno": 2, "text": WORKING[1]},
    {"kind": "add", "old_lineno": None, "new_lineno": 3, "text": WORKING[2]},
    {"kind": "add", "old_lineno": None, "new_lineno": 4, "text": WORKING[3]},
    *({"kind": "context", "old_lineno": n - 2, "new_lineno": n, "text": WORKING[n - 1]} for n in range(5, 10)),
]
HEADER = "@@ -1,2 +1,4 @@"


def _hunk() -> Hunk:
    return Hunk(
        index=0,
        file_path="app.py",
        header=HEADER,
        lines=[HEADER, " def total(price, qty):", "-    return price * qty * 1.2", "+    tax = 0.2"],
        full_lines=FULL_LINES,
        highlight_start=1,
        highlight_end=4,
    )


def _marked(*new_linenos: int) -> list[dict]:
    rows = [line for line in FULL_LINES if line["new_lineno"] in new_linenos]
    return [{"file_path": "app.py", **row} for row in rows]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("\n".join(WORKING) + "\n", encoding="utf-8")
    return tmp_path


def _comment(repo: Path, marked: list[dict] | None, **extra) -> dict:
    return {
        "id": 1,
        "file_path": "app.py",
        "where": "lines 3-4",
        "instruction": "Inline subtotal",
        "severity": "suggestion",
        **comment_location([_hunk()], _hunk(), marked),
        **extra,
    }


# --- what a queued comment records ---


def test_marked_lines_record_their_kinds_context_and_hunk():
    location = comment_location([_hunk()], _hunk(), _marked(3, 4))
    assert location["code"] == [{"kind": "add", "text": WORKING[2]}, {"kind": "add", "text": WORKING[3]}]
    assert [line["kind"] for line in location["context_before"]] == ["context", "del", "add"]
    assert [line["text"] for line in location["context_after"]] == ["", "def other():", "    pass"]
    assert location["hunk_header"] == HEADER
    assert location["lines"] == {"old_start": None, "old_end": None, "new_start": 3, "new_end": 4}


def test_a_whole_hunk_comment_covers_the_hunks_changed_lines():
    location = comment_location([_hunk()], _hunk(), None)
    assert [line["kind"] for line in location["code"]] == ["del", "add", "add", "add"]
    assert location["lines"] == {"old_start": 2, "old_end": 2, "new_start": 2, "new_end": 4}
    assert location["context_before"] == [{"kind": "context", "text": WORKING[0]}]


def test_a_mark_missing_from_the_diff_keeps_what_the_client_sent():
    stale = [{"file_path": "app.py", "old_lineno": 90, "new_lineno": 91, "text": "gone", "kind": "add"}]
    location = comment_location([_hunk()], _hunk(), stale)
    assert location["code"] == [{"kind": "add", "text": "gone"}]
    assert location["hunk_header"] is None and location["context_before"] == []


def test_request_change_stores_the_location(repo):
    session = Session([_hunk()], None, str(repo))
    session.review_started = True
    session.index = 0

    class Socket:
        async def send_json(self, data):
            pass

    asyncio.run(comments.handle_request_change(Socket(), session, {"text": "Inline it", "marked_lines": _marked(3)}))

    [stored] = session.pending_review_comments
    assert stored["code"] == [{"kind": "add", "text": WORKING[2]}]
    assert stored["hunk_header"] == HEADER and stored["created_at"]
    assert stored["anchor"] is not None and stored["snippet"] == WORKING[2]  # still there for the UI


# --- the staleness check ---


def test_unchanged_code_is_ok(repo):
    assert check_status(str(repo), _comment(repo, _marked(3, 4))) == ("ok", None)


def test_code_pushed_down_the_file_is_moved(repo):
    (repo / "app.py").write_text("\n".join(["# header", "", *WORKING]) + "\n", encoding="utf-8")
    assert check_status(str(repo), _comment(repo, _marked(3, 4))) == ("moved", {"new_start": 5, "new_end": 6})


def test_edited_code_is_changed(repo):
    (repo / "app.py").write_text("\n".join(WORKING).replace("subtotal = ", "sub = ") + "\n", encoding="utf-8")
    assert check_status(str(repo), _comment(repo, _marked(3, 4))) == ("changed", None)


def test_a_deleted_file_is_reported(repo):
    (repo / "app.py").unlink()
    assert check_status(str(repo), _comment(repo, _marked(3, 4))) == ("file_deleted", None)


def test_removed_lines_alone_cannot_be_checked(repo):
    removed_only = [{"file_path": "app.py", **FULL_LINES[1]}]
    assert check_status(str(repo), _comment(repo, removed_only)) == ("not_checked", None)


def test_a_comment_queued_before_this_format_still_works(repo):
    """session_state.json from an older version has no "code"."""
    legacy = {"id": 7, "file_path": "app.py", "where": "line 3", "instruction": "x", "snippet": WORKING[2]}
    assert check_status(str(repo), legacy) == ("not_checked", None)
    [entry] = build_review([legacy], "", str(repo), None, "2026-09-25T10:00:00")["comments"]
    assert entry["snippet"] == WORKING[2]
    assert WORKING[2] in render_plan(build_review([legacy], "", str(repo), None, "2026-09-25T10:00:00"))


# --- the structured review and the plan ---


def _three(repo: Path) -> list[dict]:
    return [
        _comment(repo, _marked(2), id=1, severity="nit", instruction="Name the constant"),
        _comment(repo, _marked(3, 4), id=2, severity="must-fix", instruction="Tax is applied twice"),
        _comment(repo, None, id=3, severity="suggestion", instruction="Add a test\nfor zero qty"),
    ]


def test_review_orders_by_severity_and_leaves_out_ui_fields(repo):
    queue = [dict(c, anchor={"first_new_lineno": 2}, snippet="x") for c in _three(repo)]
    review = build_review(queue, "Looks close", str(repo), "abc1234def5678", "2026-09-25T10:00:00")
    assert [c["severity"] for c in review["comments"]] == ["must-fix", "suggestion", "nit"]
    assert all("anchor" not in c and "snippet" not in c for c in review["comments"])
    assert review["base_commit"] == "abc1234def5678" and review["overall_note"] == "Looks close"
    assert all(c["status"] == "ok" for c in review["comments"])
    json.dumps(review)  # must serialise as-is


def test_plan_has_instructions_ordered_items_and_no_whole_files(repo):
    plan = render_plan(build_review(_three(repo), "", str(repo), "abc1234def5678", "2026-09-25T10:00:00"))
    assert "## How to use this plan" in plan and "Wait for approval" in plan
    assert plan.index("## Must fix") < plan.index("## Suggestions") < plan.index("## Nits")
    assert "### 1. app.py" in plan and "- [ ] Done" in plan
    assert "+    subtotal = price * qty" in plan and "-    return price * qty * 1.2" in plan
    assert "> Add a test\n> for zero qty" in plan
    assert "FAR_AWAY_MARKER" not in plan  # context only, never the whole file
    # The commented lines stand apart from the lines around them.
    nit = plan[plan.index("## Nits") :]
    assert nit.index("+    tax = 0.2") < nit.index("Surrounding lines") < nit.index("+    subtotal")
    assert plan.rstrip().endswith("shows no changes beyond what the comments asked for")


def test_plan_flags_stale_items(repo):
    queue = _three(repo)
    (repo / "app.py").write_text("\n".join(WORKING).replace("subtotal = ", "sub = ") + "\n", encoding="utf-8")
    plan = render_plan(build_review(queue, "", str(repo), None, "2026-09-25T10:00:00"))
    assert "**Stale:** the code this comment points at has changed" in plan


def test_a_moved_single_line_reads_naturally(repo):
    queue = [_comment(repo, _marked(2))]
    (repo / "app.py").write_text("\n".join(["# header", *WORKING]) + "\n", encoding="utf-8")
    plan = render_plan(build_review(queue, "", str(repo), None, "2026-09-25T10:00:00"))
    assert "(now at line 3)" in plan


# --- the handler ---


def test_finish_review_writes_one_plan_and_clears_the_queue(repo):
    session = Session([_hunk()], None, str(repo))
    session.pending_review_comments = _three(repo)
    sent: list[dict] = []

    class Socket:
        async def send_json(self, data):
            sent.append(data)

    asyncio.run(comments.handle_finish_review(Socket(), session, {"note": "Nearly there"}))

    [message] = sent
    assert message["type"] == "review_finished"
    payload = message["payload"]
    plan_path = Path(payload["plan_path"])
    assert plan_path.parent == (repo / ".review").resolve()
    assert plan_path.name.startswith("review_") and plan_path.suffix == ".md"
    plan_text = plan_path.read_text(encoding="utf-8")
    assert "Nearly there" in plan_text
    assert "review.json" not in plan_text
    # Nothing to copy unasked: the plan's text isn't sent at all.
    assert "text" not in payload
    # The plan is the only hand-off file (session_state.json is the queue's own).
    assert sorted(p.name for p in (repo / ".review").iterdir()) == sorted([plan_path.name, "session_state.json"])
    assert payload["instruction_line"] == f"Read .review/{plan_path.name} and follow its instructions"
    assert payload["comment_count"] == 3
    assert session.pending_review_comments == []


def test_two_finishes_in_one_second_do_not_overwrite(repo, monkeypatch):
    class Frozen:
        @staticmethod
        def now():
            from datetime import datetime

            return datetime(2026, 9, 25, 10, 0, 0)

    monkeypatch.setattr(comments, "datetime", Frozen)
    paths = []

    class Socket:
        async def send_json(self, data):
            paths.append(data["payload"]["plan_path"])

    for _ in range(2):
        session = Session([_hunk()], None, str(repo))
        session.pending_review_comments = _three(repo)[:1]
        asyncio.run(comments.handle_finish_review(Socket(), session, {}))
    assert len(set(paths)) == 2


# --- viewing the plan in the app ---


def test_finish_review_names_the_plan_for_the_preview(repo):
    session = Session([_hunk()], None, str(repo))
    session.pending_review_comments = _three(repo)
    sent: list[dict] = []

    class Socket:
        async def send_json(self, data):
            sent.append(data)

    asyncio.run(comments.handle_finish_review(Socket(), session, {}))
    plan_file = sent[0]["payload"]["plan_file"]
    assert plan_file.startswith(".review/review_") and plan_file.endswith(".md")
    assert (repo / plan_file).is_file()


def test_the_plan_has_no_html_the_preview_would_show_literally(repo):
    plan = render_plan(build_review(_three(repo), "", str(repo), None, "2026-09-25T10:00:00"))
    assert "<sub>" not in plan and "</sub>" not in plan
    assert f"Hunk: `{HEADER}`" in plan


def test_latest_review_plan_is_the_newest_one(repo):
    assert latest_review_plan(str(repo)) is None
    review_dir = repo / ".review"
    review_dir.mkdir()
    for name in ("review_20260925_100000.md", "review_20260925_100000_2.md", "review_20260924_235959.md"):
        (review_dir / name).write_text("x", encoding="utf-8")
    (review_dir / "session_state.json").write_text("{}", encoding="utf-8")
    assert latest_review_plan(str(repo)) == ".review/review_20260925_100000_2.md"


def test_the_summary_screen_offers_the_newest_plan(repo):
    from app.web.progress import send_summary_screen

    (repo / ".review").mkdir()
    (repo / ".review" / "review_20260925_100000.md").write_text("x", encoding="utf-8")
    sent: list[dict] = []

    class Socket:
        async def send_json(self, data):
            sent.append(data)

    asyncio.run(send_summary_screen(Socket(), Session([_hunk()], None, str(repo))))
    presenting = next(m["payload"] for m in sent if m["type"] == "presenting")
    assert presenting["review_plan"] == ".review/review_20260925_100000.md"


# --- the apply-review agent skill ---


def _finish(repo: Path, payload: dict) -> dict:
    session = Session([_hunk()], None, str(repo))
    session.pending_review_comments = _three(repo)
    sent: list[dict] = []

    class Socket:
        async def send_json(self, data):
            sent.append(data)

    asyncio.run(comments.handle_finish_review(Socket(), session, payload))
    return sent[0]["payload"]


SKILL_FILE = Path(".claude/skills/apply-review/SKILL.md")


def test_render_skill_has_frontmatter_marker_and_the_plan():
    skill = render_skill("# Review plan — x\n\nbody\n", "2026-09-25T10:00:00")
    assert skill.startswith("---\nname: apply-review\ndescription: ")
    assert "2026-09-25T10:00:00" in skill.split("---")[1]
    assert SKILL_MARKER in skill
    assert skill.rstrip().endswith("body")


def test_as_skill_writes_the_skill_and_the_instruction_is_the_command(repo):
    payload = _finish(repo, {"as_skill": True})
    skill = (repo / SKILL_FILE).read_text(encoding="utf-8")
    assert payload["skill_written"] is True and payload["skill_note"] is None
    assert payload["instruction_line"] == "/apply-review"
    assert "Tax is applied twice" in skill  # the plan itself is inside


def test_without_as_skill_no_skill_is_written(repo):
    payload = _finish(repo, {})
    assert not (repo / SKILL_FILE).exists()
    assert payload["as_skill"] is False and payload["skill_written"] is False
    assert payload["instruction_line"].startswith("Read .review/review_")


def test_the_skill_is_replaced_on_the_next_plan(repo):
    _finish(repo, {"as_skill": True, "note": "NOTE-ONE"})
    _finish(repo, {"as_skill": True, "note": "NOTE-TWO"})
    skill = (repo / SKILL_FILE).read_text(encoding="utf-8")
    assert "NOTE-TWO" in skill and "NOTE-ONE" not in skill


def test_a_users_own_apply_review_skill_is_kept(repo):
    own = repo / SKILL_FILE
    own.parent.mkdir(parents=True)
    own.write_text("---\nname: apply-review\ndescription: mine\n---\nmy own steps\n", encoding="utf-8")

    payload = _finish(repo, {"as_skill": True})

    assert own.read_text(encoding="utf-8").endswith("my own steps\n")
    assert payload["skill_written"] is False and "left as it is" in payload["skill_note"]
    assert payload["instruction_line"].startswith("Read .review/review_")  # falls back to the file
    assert any((repo / ".review").glob("review_*.md"))  # the plan itself was still saved


def test_as_skill_must_be_a_real_true(repo):
    """A string from a hand-crafted message doesn't count as opting in."""
    _finish(repo, {"as_skill": "yes"})
    assert not (repo / SKILL_FILE).exists()


def test_the_preview_carries_the_raw_plan_for_copy_plan(repo, monkeypatch):
    """The preview's Copy plan button copies md_preview's "text", so it
    works for a plan reopened after a reload, not only a fresh one."""
    from app.handlers import voice
    from app.web.config import CONFIG

    monkeypatch.setitem(CONFIG, "server", {**CONFIG["server"], "repo_path": str(repo)})
    (repo / ".review").mkdir()
    plan = "# Review plan\n\n- [ ] Done\n"
    (repo / ".review" / "review_20260925_100000.md").write_text(plan, encoding="utf-8")
    sent: list[dict] = []

    class Socket:
        async def send_json(self, data):
            sent.append(data)

    session = Session([_hunk()], None, str(repo))
    asyncio.run(voice.handle_open_md_preview(Socket(), session, {"file_path": ".review/review_20260925_100000.md"}))
    [message] = sent
    assert message["type"] == "md_preview" and message["payload"]["text"] == plan
