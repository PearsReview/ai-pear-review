"""Empty-state messaging added during the UX pass — a blank #code-view
used to read as broken rather than done. Exercised by calling onPresenting
directly with a done:true payload (see review-flow.js) rather than needing to
actually page through to the end of a real diff — this targets the exact
client-side branch the fix touched."""

from __future__ import annotations


def test_all_hunks_reviewed_shows_a_message_not_a_blank_box(page):
    page.evaluate("() => __app.onPresenting({ done: true, index: 1, total: 1 })")
    page.wait_for_timeout(150)
    empty = page.locator(".code-view-empty")
    assert empty.count() == 1
    assert "reviewed" in empty.inner_text().lower()


def test_definition_peek_with_no_lines_shows_a_message(page):
    page.evaluate("""() => __app.renderCodeView([], 0, 0, "sample.py")""")
    page.wait_for_timeout(150)
    assert page.locator(".code-view-empty").count() == 1


# --- "this repo has nothing to review at all" -------------------------------
# A distinct state from "you've reviewed everything", and the one the UI used
# to get wrong: with zero hunks, Prev/Next stayed live and clicking one
# painted the done screen over the server's "No changes found to review"
# error, "Back to hunk" offered a way back to a hunk that never existed, and
# the sidebar headed an empty list with "Changed files".
#
# Driven through __app.updateReviewProgress rather than a second app_server
# on a pristine repo: total is the only input any of this reads, and the
# scratch repo exists precisely to have changes in it (see _seed_scratch_repo).
_NO_HUNKS_PROGRESS = (
    "{ reviewed_count: 0, total: 0, current_reviewed: false, files: [], review_started: false, review_ended: false }"
)
_ONE_HUNK_PROGRESS = (
    "{ reviewed_count: 0, total: 1, current_reviewed: false,"
    " files: [{ file_path: 'sample.py', hunk_count: 1, reviewed_count: 0, first_index: 0 }],"
    " review_started: true, review_ended: false }"
)


def test_hunk_nav_is_disabled_when_there_are_no_hunks(page):
    assert not page.locator("#next-btn").is_disabled(), "precondition: the scratch repo has hunks"

    page.evaluate(f"() => __app.updateReviewProgress({_NO_HUNKS_PROGRESS})")
    page.wait_for_timeout(150)
    assert page.locator("#next-btn").is_disabled()
    assert page.locator("#prev-btn").is_disabled()

    # Not a one-way latch — a Refresh Diff that finds changes again has to
    # hand navigation back (reenableActionButtons re-reads the same count).
    page.evaluate(f"() => __app.updateReviewProgress({_ONE_HUNK_PROGRESS})")
    page.wait_for_timeout(150)
    assert not page.locator("#next-btn").is_disabled()
    assert not page.locator("#prev-btn").is_disabled()


def test_back_to_hunk_stays_hidden_when_there_are_no_hunks(page):
    page.evaluate(f"() => __app.updateReviewProgress({_NO_HUNKS_PROGRESS})")
    page.evaluate(
        """() => __app.onMdPreview({ file_path: "notes.md", content_hash: "x",
             blocks: [{ kind: "paragraph", start_line: 1, end_line: 1,
                        spans: [{ text: "hello", style: "plain" }] }] })"""
    )
    page.wait_for_timeout(150)
    assert "hidden" in (page.locator("#back-btn").get_attribute("class") or "")


def test_changed_files_heading_is_hidden_when_nothing_changed(page):
    heading = page.locator("#files-menu-label")
    page.evaluate(f"() => __app.updateReviewProgress({_ONE_HUNK_PROGRESS})")
    page.wait_for_timeout(150)
    assert "Changed files" in heading.inner_text()

    page.evaluate(f"() => __app.updateReviewProgress({_NO_HUNKS_PROGRESS})")
    page.wait_for_timeout(150)
    assert "hidden" in (heading.get_attribute("class") or "")


def test_no_hunks_done_screen_does_not_claim_everything_was_reviewed(page):
    page.evaluate("() => __app.onPresenting({ done: true, index: -1, total: 0 })")
    page.wait_for_timeout(150)
    text = page.locator(".code-view-empty").inner_text().lower()
    assert "nothing to review" in text
    assert "reviewed" not in text
