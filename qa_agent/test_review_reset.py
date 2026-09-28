"""Two related pieces that build on the review_started/review_ended gate
(see conftest.py's module docstring and test_review_persistence.py):

- The inline "+" add-comment button (static/js/code-view.js's .line-comment-add) is
  a review action like "Mark as reviewed"/"Review all" — it should be
  locked behind the same review_started gate, which it was missed by when
  that gate first shipped.
- "new_review" (empty payload) is the one way review_started/review_ended
  ever go back to False — offered via a "Start New Review" button on the
  end-of-review summary screen — and must never touch past
  .review/review_*.md hand-off plans (see handle_finish_review),
  which live in the same .review/ directory as the persisted
  session_state.json this whole feature reads/writes.

Builds its own raw page setup (not the `page`/`page_with_ws` fixtures),
same reasoning as test_review_persistence.py: those fixtures already
click past Start Review, which would hide exactly the pre-start state
these tests need to inspect.
"""

from __future__ import annotations

from pathlib import Path

from .conftest import _suppress_auto_tour


def _open_page(app_server, playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    page.goto(app_server, wait_until="load")
    page.wait_for_selector("#status-bar span", timeout=15000)
    return browser, page


def _wait_for_summary(page, timeout=30000):
    page.wait_for_selector("#code-view .code-view-empty", timeout=timeout)


def test_comment_button_locked_before_start_and_unlocked_after(app_server, playwright):
    browser, page = _open_page(app_server, playwright)
    try:
        page.wait_for_selector("#start-review-btn", timeout=15000)

        line = page.locator(".code-line").first
        line.hover()
        assert not page.locator(".line-comment-add").first.is_visible(), (
            "the add-comment button must not be interactable before the review has started"
        )

        page.click("#start-review-btn")
        page.wait_for_selector("#transcript .turn.presenter, #transcript .turn.system", timeout=30000)

        line.hover()
        add_btn = page.locator(".line-comment-add").first
        assert add_btn.is_visible(), "the add-comment button should be usable once the review has started"
        add_btn.click()
        page.fill(".line-comment-composer-textarea", "qa_agent: comment after start.")
        page.click(".line-comment-send-btn")
        page.wait_for_timeout(400)
        assert page.locator(".line-comment-badge").count() == 1
    finally:
        browser.close()


def test_start_new_review_resets_state_but_preserves_finished_document(app_server, playwright, scratch_repo: Path):
    # scratch_repo is session-scoped and shared with every other test file
    # (see conftest.py) — other tests (e.g. test_inline_comments.py's
    # Finish Review test) may have already left .review/review_*.md files
    # here, on purpose, since nothing ever cleans those up. So this checks
    # "one new file appeared, and nothing already there disappeared,"
    # never an absolute count.
    review_dir = scratch_repo / ".review"
    md_files_at_start = set(review_dir.glob("review_*.md")) if review_dir.exists() else set()

    browser, page = _open_page(app_server, playwright)
    try:
        page.wait_for_selector("#start-review-btn", timeout=15000)
        page.click("#start-review-btn")
        page.wait_for_selector("#end-review-btn", timeout=30000)

        # Queue and finish one comment so there's a real, freshly-written
        # .review/review_*.md on disk to check survives the reset below.
        line = page.locator(".code-line").first
        line.hover()
        page.locator(".line-comment-add").first.click()
        page.fill(".line-comment-composer-textarea", "qa_agent: preserved across new_review.")
        page.click(".line-comment-send-btn")
        page.wait_for_timeout(400)

        page.click("#finish-review-btn")
        page.wait_for_selector("#handoff-dialog[open]", timeout=3000)
        page.click("#handoff-create-btn")
        page.wait_for_timeout(600)

        md_files_after_finish = set(review_dir.glob("review_*.md"))
        new_files = md_files_after_finish - md_files_at_start
        assert len(new_files) == 1, "finishing the queued comment should have written exactly one new hand-off doc"

        page.click("#end-review-btn")
        _wait_for_summary(page)

        page.click("#new-review-btn")
        page.wait_for_selector("#start-review-btn", timeout=15000)
        assert not page.locator("#code-view .code-view-empty").is_visible(), (
            "Start New Review should replace the summary with a real hunk again"
        )

        md_files_after_reset = set(review_dir.glob("review_*.md"))
        assert md_files_after_reset == md_files_after_finish, (
            "new_review must never touch past .review/review_*.md hand-off plans"
        )
    finally:
        browser.close()
