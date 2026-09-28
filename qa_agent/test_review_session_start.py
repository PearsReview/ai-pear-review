"""The review_started gate (app/handlers/narration.py's present_current_hunk /
"start_review") — briefing/conversation must not fire until the reviewer
explicitly starts the review, even though the code view for hunk 0 still
loads automatically on connect exactly as it did before this gate existed.

Builds its own page/WS-capture setup rather than using the `page`/
`page_with_ws` fixtures from conftest.py, deliberately: those fixtures
already click past the toolbar's "Start Review" button (see
_start_review_if_gated) so every other test in this suite keeps its
pre-gate assumption that the first hunk's narration is already showing —
exactly the thing these tests need to *not* have happened yet.
"""

from __future__ import annotations

from .conftest import _suppress_auto_tour
from .ws_capture import WsFrames


def test_fresh_connect_shows_code_but_defers_narration(app_server, playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    frames = WsFrames()
    frames.attach(page)
    try:
        page.goto(app_server, wait_until="load")
        page.wait_for_selector("#status-bar span", timeout=15000)

        # Code view loads immediately — unaffected by the gate.
        page.wait_for_selector("#code-view .code-line", timeout=15000)
        # The toolbar button shows in place of a "...thinking" spinner or
        # real narration.
        page.wait_for_selector("#start-review-btn", timeout=15000)

        assert page.locator("#transcript .turn.presenter").count() == 0, (
            "no persona turn should exist before the review is started"
        )
        assert not any(f.get("type") == "narration" for f in frames.frames), (
            "no narration WS frame should be sent before start_review"
        )
    finally:
        browser.close()


def test_start_review_button_triggers_exactly_one_narration(app_server, playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    frames = WsFrames()
    frames.attach(page)
    try:
        page.goto(app_server, wait_until="load")
        page.wait_for_selector("#status-bar span", timeout=15000)
        page.wait_for_selector("#start-review-btn", timeout=15000)

        page.click("#start-review-btn")
        # 60s, matching test_semantic_quality's FRESH_NARRATION_TIMEOUT_MS.
        # This click starts two chained model calls — a briefing with
        # nothing cached, then the narration — measured at roughly 22s plus
        # 15s. It is also usually the first narration of the whole session,
        # so it can additionally pay Ollama's model load. 30s used to be
        # enough by luck rather than by margin, and stopped being.
        frames.wait_for("narration", page, timeout_ms=60_000)
        page.wait_for_timeout(500)

        narration_frames = [f for f in frames.frames if f.get("type") == "narration"]
        assert len(narration_frames) == 1, "exactly one narration frame should follow the click"
        assert page.locator("#transcript .turn.presenter").count() == 1
        assert not page.locator("#start-review-btn").is_visible(), (
            "the button is gone once clicked — nothing left to start or resume"
        )
    finally:
        browser.close()


def test_browsing_before_start_does_not_trigger_narration(app_server, playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    frames = WsFrames()
    frames.attach(page)
    try:
        page.goto(app_server, wait_until="load")
        page.wait_for_selector("#status-bar span", timeout=15000)
        page.wait_for_selector("#start-review-btn", timeout=15000)

        # scratch_repo has exactly one hunk (see conftest.py), so Next goes
        # straight to "All hunks reviewed" and Prev returns to hunk 0 —
        # code-only browsing, same as before the gate existed, and neither
        # step should ever fire briefing/conversation on its own.
        page.click("#next-btn")
        page.wait_for_timeout(300)
        page.click("#prev-btn")
        page.wait_for_timeout(500)

        assert not any(f.get("type") == "narration" for f in frames.frames), (
            "browsing hunks before start_review must not trigger narration"
        )
        assert page.locator("#transcript .turn.presenter").count() == 0
    finally:
        browser.close()
