"""Review progress (review_started/review_ended, reviewed-hunk marks,
queued comments — see app/services/session_store.py) now survives both a
reconnect and, in principle, a full app restart, since it's read from and
written to {repo_path}/.review/session_state.json rather than living only
in the per-connection Session. This suite exercises the read/write path
via two *separate* WebSocket connections against the same running
app_server — a lighter, faster proxy for "restart the whole process" that
still exercises the real disk round-trip (websocket_endpoint's
load_persisted_state call), and the End Review action that also depends
on it.

Builds its own page setup rather than using the `page`/`page_with_ws`
fixtures from conftest.py, deliberately: those fixtures already click
past "Start Review" for every test (see _start_review_if_gated), which
would hide exactly the pre-start/post-end states these tests need to
inspect on a fresh connection.

The scratch repo (see conftest.py) has exactly one hunk, so "mark this
hunk reviewed" and "mark every hunk reviewed" are the same action here —
that's what makes handle_toggle_reviewed's 100%-reviewed auto-end path
directly testable without a multi-hunk fixture.
"""

from __future__ import annotations

from .conftest import _suppress_auto_tour


def _open_page(app_server, playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    page.goto(app_server, wait_until="load")
    page.wait_for_selector("#status-bar span", timeout=15000)
    return browser, page


def _wait_for_summary(page, timeout=30000):
    """The review-ended screen renders via showEmptyCodeView (see
    onPresenting's payload.ended branch), same as the plain "All hunks
    reviewed" screen — this class is the reliable signal either one has
    replaced the code view, without racing a fixed sleep against the
    server round-trip."""
    page.wait_for_selector("#code-view .code-view-empty", timeout=timeout)


def test_review_started_persists_across_reconnect(app_server, playwright):
    browser_a, page_a = _open_page(app_server, playwright)
    try:
        page_a.wait_for_selector("#start-review-btn", timeout=15000)
        page_a.click("#start-review-btn")
        page_a.wait_for_selector("#transcript .turn.presenter, #transcript .turn.system", timeout=30000)
    finally:
        browser_a.close()

    browser_b, page_b = _open_page(app_server, playwright)
    try:
        # A genuine resume: narration for the (only) hunk shows up without
        # any reviewer action — no "Start Review" to click at all. Wait
        # for narration first so the visibility check below reflects the
        # server's real review_started value, not just the button's
        # hidden-by-default HTML state before "presenting" has arrived.
        page_b.wait_for_selector("#transcript .turn.presenter, #transcript .turn.system", timeout=30000)
        assert not page_b.locator("#start-review-btn").is_visible()
        assert page_b.locator("#code-view .code-line").count() > 0, (
            "code view should still show the real hunk, not the summary"
        )
    finally:
        browser_b.close()


def test_end_review_shows_summary_and_locks_controls(app_server, playwright):
    browser, page = _open_page(app_server, playwright)
    try:
        page.wait_for_selector("#start-review-btn", timeout=15000)
        page.click("#start-review-btn")
        page.wait_for_selector("#end-review-btn", timeout=30000)

        page.click("#end-review-btn")
        _wait_for_summary(page)

        assert "Review ended" in page.locator("#hunk-meta").inner_text(), (
            "ending before marking anything reviewed is the early-end wording"
        )
        for selector in ("#start-review-btn", "#end-review-btn", "#reviewed-btn", "#review-all-toggle"):
            assert not page.locator(selector).is_visible(), f"{selector} should be hidden once the review has ended"
    finally:
        browser.close()


def test_marking_the_only_hunk_reviewed_auto_ends(app_server, playwright):
    browser, page = _open_page(app_server, playwright)
    try:
        page.wait_for_selector("#start-review-btn", timeout=15000)
        page.click("#start-review-btn")
        page.wait_for_selector("#reviewed-btn", timeout=30000)

        page.click("#reviewed-btn")  # the scratch repo's only hunk -> 100% reviewed
        _wait_for_summary(page)

        assert "Review complete" in page.locator("#hunk-meta").inner_text(), (
            "reaching 100% reviewed is the natural-completion wording, not early-end"
        )
        assert not page.locator("#reviewed-btn").is_visible()
    finally:
        browser.close()


def test_reconnect_after_ended_lands_on_summary(app_server, playwright):
    browser_a, page_a = _open_page(app_server, playwright)
    try:
        page_a.wait_for_selector("#start-review-btn", timeout=15000)
        page_a.click("#start-review-btn")
        page_a.wait_for_selector("#end-review-btn", timeout=30000)
        page_a.click("#end-review-btn")
        _wait_for_summary(page_a)
    finally:
        browser_a.close()

    browser_b, page_b = _open_page(app_server, playwright)
    try:
        _wait_for_summary(page_b)
        assert "Review ended" in page_b.locator("#hunk-meta").inner_text(), (
            "a reconnect after ending should land straight on the summary, not hunk 0"
        )
        assert not page_b.locator("#start-review-btn").is_visible()
        assert page_b.locator("#code-view .code-line").count() == 0, (
            "the summary screen replaces the code view, it doesn't sit behind it"
        )
    finally:
        browser_b.close()


# --- Browsing after the review has ended ---------------------------------
#
# present_current_hunk no longer redirects every navigation message to the
# summary screen once review_ended is set (see its docstring in app/handlers/narration.py)
# — Prev/Next/a file click now actually show the requested hunk. These
# tests are the ones that would have caught the original bug: clicking a
# file after ending used to send a real WS message, get a real response,
# and that response was always the identical summary screen — indistin-
# guishable, from the reviewer's side, from the click doing nothing.


def _end_review(page):
    page.wait_for_selector("#start-review-btn", timeout=15000)
    page.click("#start-review-btn")
    page.wait_for_selector("#end-review-btn", timeout=30000)
    page.click("#end-review-btn")
    _wait_for_summary(page)


def test_next_actually_shows_a_hunk_after_review_ends(app_server, playwright, richer_scratch_repo):
    """The exact bug reported: after ending, navigation used to be a dead
    click. richer_scratch_repo gives 4 hunks across 3 files, so there's
    somewhere for Next to actually go."""
    browser, page = _open_page(app_server, playwright)
    try:
        _end_review(page)
        page.click("#next-btn")
        page.wait_for_selector("#code-view .code-line", timeout=15000)
        assert "Hunk" in page.locator("#hunk-meta").inner_text(), (
            "Next after ending must show a real hunk, not stay stuck on the summary"
        )
        assert page.locator("#code-view .code-line").count() > 0
    finally:
        browser.close()


def test_a_file_click_after_review_ends_shows_that_files_hunk(app_server, playwright, richer_scratch_repo):
    browser, page = _open_page(app_server, playwright)
    try:
        _end_review(page)
        page.click("#file-sidebar-tab")
        page.wait_for_timeout(300)
        pills = page.locator(".file-pill")
        target_label = None
        for i in range(pills.count()):
            label = pills.nth(i).inner_text()
            if "feature.py" in label:
                target_label = label
                pills.nth(i).click()
                break
        assert target_label is not None, "richer_scratch_repo's feature.py should be in the file list"
        page.wait_for_selector("#code-view .code-line", timeout=15000)
        assert "feature.py" in page.locator("#hunk-meta").inner_text()
    finally:
        browser.close()


def test_mark_as_reviewed_and_composer_stay_locked_while_browsing_post_end(app_server, playwright, richer_scratch_repo):
    """The gap a naive fix would leave open: onPresenting's real-hunk
    branch used to assume review_ended could never be true there, and
    unconditionally re-showed Mark-as-reviewed/Review-all and re-enabled
    the composer. Browsing after ending must not reopen either."""
    browser, page = _open_page(app_server, playwright)
    try:
        _end_review(page)
        page.click("#next-btn")
        page.wait_for_selector("#code-view .code-line", timeout=15000)

        for selector in ("#reviewed-btn", "#review-all-toggle", "#start-review-btn", "#end-review-btn"):
            assert not page.locator(selector).is_visible(), f"{selector} must stay hidden while browsing post-end"
        assert page.locator("#text-input").is_disabled()
        assert page.locator("#send-btn").is_disabled()
        assert page.locator("#act-now-btn").is_disabled()
        assert "comments-locked" in (page.locator("#code-view").get_attribute("class") or "")
    finally:
        browser.close()


def test_back_to_summary_button_appears_and_returns_to_the_wrapup(app_server, playwright, richer_scratch_repo):
    browser, page = _open_page(app_server, playwright)
    try:
        _end_review(page)
        assert page.locator("#back-to-summary-btn").is_hidden(), "not shown while the summary itself is on screen"

        page.click("#next-btn")
        page.wait_for_selector("#code-view .code-line", timeout=15000)
        assert page.locator("#back-to-summary-btn").is_visible()

        page.click("#back-to-summary-btn")
        _wait_for_summary(page)
        assert page.locator("#back-to-summary-btn").is_hidden()
    finally:
        browser.close()


def test_reply_is_rejected_even_if_sent_directly_while_ended(app_server, playwright, richer_scratch_repo):
    """Defense-in-depth check for handle_reply's own guard (app/handlers/narration.py)
    — bypasses the disabled composer entirely by calling the page's own
    send() function directly, the way a crafted or stale client could,
    rather than trusting that the UI alone keeps this locked."""
    browser, page = _open_page(app_server, playwright)
    try:
        _end_review(page)
        page.click("#next-btn")
        page.wait_for_selector("#code-view .code-line", timeout=15000)

        page.evaluate("__app.send('reply', {text: 'is this still active?'})")
        page.wait_for_selector("#error-banner:not(.hidden)", timeout=10000)
        assert "ended" in page.locator("#error-banner").inner_text().lower()
        assert page.locator("#transcript .turn.presenter").count() == 0, "no reply should have been generated"
    finally:
        browser.close()
