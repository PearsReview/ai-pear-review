"""Two paths whose *output* nobody was judging, even though both are
assembled server-side and handed to a model or a human:

- **Marked-lines context.** Selecting lines injects them into the next
  reply's prompt as hidden context (app/web/context.py's marked_lines_context) —
  the reviewer's own message stays a bare "why this?". Nothing verified
  the injection actually lands, and it can't be checked from the visible
  transcript: the only evidence is whether the answer is about those
  lines. test_marking.py proves the *marks* work; this proves the
  *context* does.
- **The Finish Review hand-off plan.** Rendered from the queue (app/handlers/comments.py's
  handle_finish_review), written to .review/review_<timestamp>.md and put
  on the clipboard for another engineer or another AI session to act on.
  test_inline_comments.py proves the queue empties and a file appears;
  nothing checked the document actually still contains what was queued —
  which is the whole point of it. A comment silently dropped here loses
  the review at its very last step.
"""

from __future__ import annotations

from ..judge_prompts import (
    FINISH_REVIEW_DOC_JUDGE_SYSTEM,
    FINISH_REVIEW_DOC_JUDGE_USER,
    MARKED_CONTEXT_JUDGE_SYSTEM,
    MARKED_CONTEXT_JUDGE_USER,
)
from ..judging import judge_and_record
from ..llm_client import LLMClient
from .scenario_helpers import ask_and_capture_reply

_FRESH_NARRATION_TIMEOUT_MS = 60_000  # see test_messy_navigation.py's copy for why


def test_marked_lines_actually_reach_the_reply(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    frames.wait_for("narration", page, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)

    # Mark a 2-line range over lines that are actually part of the change
    # (.code-line.add), not the file's first lines. #code-view renders the
    # *whole* file, so nth(0)/nth(2) — what test_marking.py uses, since it
    # only cares that marking works at all — lands on unchanged context;
    # asking "why is this written this way?" about pre-existing code the
    # reviewer didn't touch is a genuinely ambiguous question, and judging
    # the answer to it says nothing about whether the injection worked.
    added = page.locator(".code-line.add")
    assert added.count() >= 1, "expected at least one added line to mark"
    added.first.dblclick()
    page.wait_for_timeout(100)
    added.nth(min(1, added.count() - 1)).dblclick()
    page.wait_for_timeout(200)
    marked_text = "\n".join(
        page.locator(".code-line.mark-range").nth(i).inner_text()
        for i in range(page.locator(".code-line.mark-range").count())
    )
    assert marked_text.strip(), "expected a marked range before asking about it"

    # Deliberately vague on its own: everything that makes this answerable
    # is in the injected context, so a reply that's about the file in
    # general rather than these lines means the injection didn't land.
    question = "Why is this written this way?"
    reviewer_turn = ask_and_capture_reply(page, frames, question)

    judge_and_record(
        category="marked_context",
        client=LLMClient(**judge_model_config),
        system_prompt=MARKED_CONTEXT_JUDGE_SYSTEM,
        user_prompt=MARKED_CONTEXT_JUDGE_USER.format(
            file_path=presenting["file_path"],
            marked_lines=marked_text,
            human_text=question,
            reply_text=reviewer_turn["text"],
        ),
        context={
            "file_path": presenting["file_path"],
            "marked_lines": marked_text,
            "human_text": question,
            "reply_text": reviewer_turn["text"],
        },
        response_text=reviewer_turn["text"],
        capture_dir=llm_capture_dir,
    )


def _queue_comment(page, text: str, line_index: int) -> None:
    """Same composer gesture as test_inline_comments.py's own helper — the
    "+" is opacity:0/pointer-events:none until its line is hovered, so the
    hover isn't optional."""
    line = page.locator(".code-line").nth(line_index)
    line.hover()
    line.locator(".line-comment-add").click()
    page.wait_for_timeout(150)
    page.fill(".line-comment-composer-textarea", text)
    page.click(".line-comment-send-btn")
    page.wait_for_timeout(400)


def test_finish_review_document_carries_every_queued_comment(page, scratch_repo, judge_model_config, llm_capture_dir):
    # Plain `page` (not page_with_ws): nothing here needs WS frames — the
    # document is read off disk, and the queue state off the DOM.
    comments = [
        "Rename this to something clearer than a single letter.",
        "This needs a test covering the empty-input case.",
    ]
    _queue_comment(page, comments[0], line_index=0)
    _queue_comment(page, comments[1], line_index=1)
    assert page.locator(".line-comment-badge").count() == 2

    # Create plan opens the hand-off dialog; confirm it with the defaults.
    review_dir = scratch_repo / ".review"
    before = {p.name for p in review_dir.glob("review_*.md")} if review_dir.exists() else set()
    page.click("#finish-review-btn")
    page.wait_for_selector("#handoff-dialog[open]", timeout=3000)
    page.click("#handoff-create-btn")
    page.wait_for_timeout(1500)

    written = sorted({p.name for p in review_dir.glob("review_*.md")} - before)
    assert written, "Finish Review should have written a hand-off plan"
    document = (review_dir / written[-1]).read_text(encoding="utf-8")

    judge_and_record(
        category="finish_review_doc",
        client=LLMClient(**judge_model_config),
        system_prompt=FINISH_REVIEW_DOC_JUDGE_SYSTEM,
        user_prompt=FINISH_REVIEW_DOC_JUDGE_USER.format(
            queued_comments="\n".join(f"- {c}" for c in comments),
            document=document,
        ),
        context={"queued_comments": comments, "document": document},
    )
