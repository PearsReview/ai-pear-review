""" "All files" explore mode — browsing and asking about a file that has no
diff hunks (see app/handlers/explore.py's handle_list_all_files/handle_explore_file/
handle_explore_reply and sidebar.js/review-flow.js's exploreAllFilesBtn/onFileExplore).
The scratch repo's `unchanged.py` (see conftest.py's _seed_scratch_repo)
is the file this suite opens — `.gitignore` is *also* tracked-and-
unchanged (seeded in the same initial commit), so every locator here
targets `unchanged.py` by name specifically rather than assuming it's the
only (or first) ".file-pill-unchanged" match.

Builds its own raw page (not the `page`/`page_with_ws` fixtures), same
reasoning as test_review_session_start.py: those fixtures already click
past Start Review, which would hide the pre-start behavior this feature
is specifically supposed to keep working.
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


def _unchanged_pill(page):
    return page.locator(".file-pill-unchanged").filter(has_text="unchanged.py")


def test_all_files_toggle_lists_an_unchanged_file(app_server, playwright):
    browser, page = _open_page(app_server, playwright)
    try:
        page.wait_for_selector("#start-review-btn", timeout=15000)  # a real "presenting" has landed

        page.click("#file-sidebar-tab")  # expand the file explorer sidebar
        assert page.locator(".file-pill-unchanged").count() == 0, "unchanged.py shouldn't show before the toggle is on"

        page.click("#explore-all-files-btn")
        page.wait_for_selector(".file-pill-unchanged", timeout=15000)
        assert _unchanged_pill(page).count() == 1
        assert page.locator("#explore-all-files-btn").get_attribute("aria-pressed") == "true"

        page.click("#explore-all-files-btn")  # either/or toggle — click again to turn it back off
        page.wait_for_timeout(200)
        assert page.locator(".file-pill-unchanged").count() == 0, "toggling off should snap back to changed-files-only"
    finally:
        browser.close()


def test_explore_file_and_back_before_start_review(app_server, playwright):
    browser, page = _open_page(app_server, playwright)
    try:
        page.wait_for_selector("#start-review-btn", timeout=15000)

        page.click("#file-sidebar-tab")
        page.click("#explore-all-files-btn")
        page.wait_for_selector(".file-pill-unchanged", timeout=15000)
        _unchanged_pill(page).click()

        page.wait_for_selector("#back-btn:not(.hidden)", timeout=15000)
        assert "unchanged.py" in page.locator("#hunk-meta").inner_text()
        assert "never touched by any test" in page.locator("#code-view").inner_text()

        page.click("#back-btn")
        page.wait_for_selector("#back-btn", state="hidden", timeout=15000)
        assert "sample.py" in page.locator("#hunk-meta").inner_text(), "Back should return to the real hunk"
    finally:
        browser.close()


def test_asking_about_unchanged_file_works_before_start_review(app_server, playwright):
    browser, page = _open_page(app_server, playwright)
    try:
        page.wait_for_selector("#start-review-btn", timeout=15000)
        # The composer is enabled pre-start now regardless of explore mode
        # (see setComposerEnabled in review-flow.js) — this test's job is just to
        # confirm explore-mode replies specifically still work before
        # Start Review, same as they did when the composer was gated.

        page.click("#file-sidebar-tab")
        page.click("#explore-all-files-btn")
        page.wait_for_selector(".file-pill-unchanged", timeout=15000)
        _unchanged_pill(page).click()
        page.wait_for_selector("#back-btn:not(.hidden)", timeout=15000)

        assert not page.locator("#send-btn").is_disabled(), "explore-mode conversation must work before Start Review"
        page.fill("#text-input", "What does this file do?")
        page.click("#send-btn")
        page.wait_for_selector("#transcript .turn.presenter", timeout=30000)
        assert page.locator("#transcript .turn.reviewer").count() == 1
    finally:
        browser.close()


def test_start_review_snaps_out_of_explore_mode(app_server, playwright):
    browser, page = _open_page(app_server, playwright)
    try:
        page.wait_for_selector("#start-review-btn", timeout=15000)

        page.click("#file-sidebar-tab")
        page.click("#explore-all-files-btn")
        page.wait_for_selector(".file-pill-unchanged", timeout=15000)
        _unchanged_pill(page).click()
        page.wait_for_selector("#back-btn:not(.hidden)", timeout=15000)

        page.click("#start-review-btn")
        page.wait_for_selector("#back-btn", state="hidden", timeout=15000)
        assert "sample.py" in page.locator("#hunk-meta").inner_text(), (
            "starting the review should show the real hunk again"
        )
        assert page.locator("#explore-all-files-btn").get_attribute("aria-pressed") == "false", (
            "the toggle should snap back off"
        )
        assert page.locator(".file-pill-unchanged").count() == 0
    finally:
        browser.close()
