"""Next/Prev navigation, and the disable-while-in-flight / reenable-on-
response behavior added during the UX pass."""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect


def test_next_past_the_last_hunk_reaches_done(page):
    # Only one hunk exists in the scratch repo's diff (see conftest's
    # _SAMPLE_MODIFIED). Next from it should advance past the end and
    # reach the "done" state (handle_next in app/handlers/review_flow.py lets the index go one
    # past the last valid hunk, at which point current_hunk is None) —
    # NOT silently re-present the same hunk, which was the previous,
    # buggy behavior this test used to assert as correct.
    first_meta = page.locator("#hunk-meta").inner_text()
    assert "sample.py" in first_meta

    page.click("#next-btn")
    page.wait_for_timeout(300)
    assert "All hunks reviewed" in page.locator("#hunk-meta").inner_text()

    # Prev from "done" recovers back to the (only) real hunk.
    page.click("#prev-btn")
    page.wait_for_timeout(300)
    assert "sample.py" in page.locator("#hunk-meta").inner_text()

    # Next past the end again is idempotent — it doesn't run away past
    # "done" on repeated clicks.
    page.click("#next-btn")
    page.wait_for_timeout(300)
    page.click("#next-btn")
    page.wait_for_timeout(300)
    assert "All hunks reviewed" in page.locator("#hunk-meta").inner_text()


def test_next_btn_disables_immediately_and_reenables(page):
    # Click + read disabled state within one synchronous JS turn, before
    # the async WebSocket response can be processed — otherwise a fast
    # local round-trip can make the disabled state look like it never
    # happened (see this session's own debugging of exactly this).
    disabled_immediately = page.evaluate(
        """() => {
            const btn = document.getElementById('next-btn');
            btn.click();
            return btn.disabled;
        }"""
    )
    assert disabled_immediately is True
    expect(page.locator("#next-btn")).not_to_be_disabled(timeout=10_000)
    expect(page.locator("#prev-btn")).not_to_be_disabled(timeout=10_000)


def test_step_into_stays_disabled_without_a_text_selection(page):
    assert page.locator("#step-into-btn").is_disabled() is True


@pytest.mark.skip(reason="Step Into's button is hidden until it covers more languages (see README, Known limitations)")
def test_step_into_disables_on_click_and_reenables_on_error(page):
    # Selecting non-identifier garbage still enables the button (selection
    # presence is all that's checked client-side); the server then fails
    # to resolve a definition for it, which should re-enable the button
    # via reenableActionButtons() in showError rather than leaving it
    # stuck disabled.
    page.evaluate(
        """() => {
            const range = document.createRange();
            const textNode = document.querySelector('#code-view .text');
            range.selectNodeContents(textNode);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            document.getElementById('code-view').dispatchEvent(new Event('mouseup', {bubbles: true}));
        }"""
    )
    time.sleep(0.2)
    assert page.locator("#step-into-btn").is_disabled() is False
    page.click("#step-into-btn")
    # Auto-retrying assertion (polls up to the timeout) rather than a fixed
    # sleep-then-check — the server round trip's actual duration varies
    # (code_search.py's lookup, error path), and a fixed wait is exactly
    # what flaked here once already.
    expect(page.locator("#step-into-btn")).not_to_be_disabled(timeout=10_000)
