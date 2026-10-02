"""Mark-as-reviewed and the inline-comment -> Finish Review hand-off,
exercised against a real hunk from target_repo's real diff. See
qa_agent/test_marking.py and qa_agent/test_inline_comments.py for the
exhaustive synthetic-repo versions of these flows — this file isn't trying
to re-cover that ground, only to confirm both still work end to end
against real content and (for Finish Review) that the hand-off document
actually lands on disk inside target_repo, not just over the wire.

Builds its own raw page + WsFrames per test rather than using
conftest.py's `reviewing_page` — Finish Review's result only ever arrives
as a `review_finished` WS frame (see app/handlers/comments.py's handle_finish_review),
never as DOM state, so every test here needs frame capture attached before
navigation the same way test_live_review.py's narrated_walk does.
"""

from __future__ import annotations

from pathlib import Path

from qa_agent.ws_capture import WsFrames

from .conftest import _suppress_auto_tour

# Same reasoning as test_live_review.py's NARRATION_TIMEOUT_MS: a hunk's
# first narration is a briefing + narration pair, up to ~40s combined on a
# CPU-only Ollama setup.
FIRST_NARRATION_TIMEOUT_MS = 120_000


def _open_reviewing_page(app_server: str, playwright):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    frames = WsFrames()
    frames.attach(page)
    page.goto(app_server, wait_until="load")
    page.wait_for_selector("#status-bar span", timeout=15000)
    page.click("#start-review-btn", timeout=10000)
    frames.wait_for("narration", page, timeout_ms=FIRST_NARRATION_TIMEOUT_MS)
    return browser, page, frames


def test_mark_reviewed_toggles_a_real_hunk(app_server: str, playwright):
    browser, page, _frames = _open_reviewing_page(app_server, playwright)
    try:
        page.wait_for_selector("#reviewed-btn:not(.hidden)", timeout=15000)
        reviewed_btn = page.locator("#reviewed-btn")
        assert reviewed_btn.get_attribute("aria-pressed") == "false"

        reviewed_btn.click()
        page.wait_for_timeout(300)
        assert reviewed_btn.get_attribute("aria-pressed") == "true"
        assert "reviewed" in (reviewed_btn.get_attribute("class") or "")

        reviewed_btn.click()  # toggle back off — leave state as found
        page.wait_for_timeout(300)
        assert reviewed_btn.get_attribute("aria-pressed") == "false"
    finally:
        browser.close()


def test_finish_review_writes_a_real_handoff_doc(app_server: str, playwright, target_repo: Path):
    browser, page, frames = _open_reviewing_page(app_server, playwright)
    try:
        comment_text = "qa_agent/live: does this need a null check here?"
        line = page.locator(".code-line").first
        line.hover()
        line.locator(".line-comment-add").click()
        page.wait_for_timeout(150)
        page.fill(".line-comment-composer-textarea", comment_text)
        page.click(".line-comment-send-btn")
        page.wait_for_timeout(400)
        assert page.locator(".line-comment-badge").count() == 1

        page.click("#finish-review-btn")
        page.wait_for_selector("#handoff-dialog[open]", timeout=3000)
        page.click("#handoff-create-btn")
        payload = frames.wait_for("review_finished", page, timeout_ms=15000)

        assert payload["comment_count"] == 1
        assert page.locator(".line-comment-badge").count() == 0

        saved_path = Path(payload["plan_path"])
        assert saved_path.is_relative_to(target_repo.resolve()), (
            f"hand-off plan saved outside target_repo: {saved_path}"
        )
        assert saved_path.exists(), f"payload named {saved_path} but nothing was written there"
        saved_text = saved_path.read_text(encoding="utf-8")
        assert comment_text in saved_text, "hand-off plan doesn't contain the comment that was queued"
    finally:
        browser.close()
