"""Accessibility basics added during the UX pass: focus-visible outline,
aria-label on icon-only controls, aria-pressed on toggle buttons, and
sane keyboard tab order."""

from __future__ import annotations

ICON_ONLY_BUTTON_SELECTORS = [
    "#theme-toggle-btn",
    "#refresh-diff-btn",
    "#clear-marks-btn",
    "#mic-btn",
    "#explore-all-files-btn",
    '.chat-tab[data-tab="overall"]',
    '.chat-tab[data-tab="file"]',
    "#interrupt-btn",
    "#act-now-btn",
    "#voice-settings-btn",
    "#send-btn",
]


def test_icon_only_buttons_have_aria_labels(page):
    for selector in ICON_ONLY_BUTTON_SELECTORS:
        label = page.locator(selector).get_attribute("aria-label")
        assert label, f"{selector} has no aria-label"


def test_reviewed_btn_exposes_pressed_state(page):
    before = page.locator("#reviewed-btn").get_attribute("aria-pressed")
    assert before == "false"
    page.click("#reviewed-btn")
    page.wait_for_timeout(300)
    after = page.locator("#reviewed-btn").get_attribute("aria-pressed")
    assert after == "true"
    # No "click it again to leave it as found" step anymore: the scratch
    # repo has exactly one hunk (see conftest.py), so marking it reviewed
    # reaches 100% and auto-ends the review (see app/handlers/review_flow.py's
    # _maybe_auto_end_review) — reviewed-btn is then locked/hidden for the
    # rest of this connection, one-way, same as review_started never
    # reverting. Harmless for later tests either way: each gets its own
    # fresh page, and persisted review state is cleared before every test
    # (see conftest.py's _clean_persisted_review_state).
    page.wait_for_selector("#code-view .code-view-empty", timeout=15000)
    assert not page.locator("#reviewed-btn").is_visible()


def test_focus_visible_outline_shows_on_keyboard_focus(page):
    page.evaluate("() => document.getElementById('theme-toggle-btn').focus()")
    page.keyboard.press("Tab")  # real keyboard traversal from a known point
    outline = page.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
    assert outline == "solid"


def test_tab_order_from_theme_toggle_follows_visual_toolbar_order(page):
    page.evaluate("() => document.getElementById('theme-toggle-btn').focus()")
    # #file-sidebar-tab replaced the old #files-menu-btn when the file
    # explorer became a collapsible panel with a hover-revealed edge tab.
    # It's still a real <button> in the natural tab order, which is what
    # this test is actually guarding.
    # end-review-btn is visible (in the tab order) here too — the `page`
    # fixture already clicks past Start Review (see _start_review_if_gated
    # in conftest.py), so by the time this test runs the review is live
    # and End Review is showing right after Review all.
    # file-sidebar-tab comes first: the toolbar now sits in the middle
    # column above the code pane, to the right of the full-height file
    # sidebar, so the sidebar's tab is visually (and in DOM order) before it.
    expected_ids = [
        "file-sidebar-tab",
        "prev-btn",
        "next-btn",
        "reviewed-btn",
        "review-all-toggle",
        "end-review-btn",
    ]
    for expected_id in expected_ids:
        page.keyboard.press("Tab")
        active_id = page.evaluate("() => document.activeElement.id")
        assert active_id == expected_id, f"expected #{expected_id}, got #{active_id}"


def test_mark_gutter_button_has_dynamic_aria_label(page):
    dot = page.locator(".mark-gutter").first
    assert dot.get_attribute("aria-label") == "Mark this line"
    dot.click()
    page.wait_for_timeout(200)
    assert dot.get_attribute("aria-label") == "Unmark this line"
    dot.click()  # leave it as found


def test_every_button_has_a_tooltip(page):
    """The toolbar goes icon-only when the window is narrow, so a button's
    title is often the only way to learn what it does. Checked across every
    button in the page, hidden ones included, plus a queued comment's so the
    generated ones are covered too."""
    line = page.locator(".code-line").first
    line.hover()
    line.locator(".line-comment-add").click()
    page.wait_for_selector(".line-comment-composer-textarea", timeout=3000)
    missing = page.evaluate(
        """() => [...document.querySelectorAll('button')]
            .filter((b) => !(b.getAttribute('title') || '').trim())
            .map((b) => b.id || b.className || b.textContent.trim().slice(0, 30))"""
    )
    assert missing == [], f"buttons with no tooltip: {missing}"
