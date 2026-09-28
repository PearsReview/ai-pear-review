"""Manual theme toggle: light -> dark -> system -> light cycle, and that
its aria-label mirrors the dynamic title text (see the UX pass)."""

from __future__ import annotations


def _current_theme(page) -> str | None:
    return page.evaluate("() => document.documentElement.getAttribute('data-theme')")


def test_theme_toggle_cycles_light_dark_system(page):
    # Start from a known state regardless of what a previous test left in
    # localStorage for this browser context.
    page.evaluate("() => localStorage.removeItem('ai_pear_review_theme')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#status-bar span", timeout=15000)

    seen = [_current_theme(page)]
    for _ in range(3):
        page.click("#theme-toggle-btn")
        page.wait_for_timeout(150)
        seen.append(_current_theme(page))

    # One full cycle back to where it started, hitting both explicit
    # states along the way (order depends on the starting point, so just
    # assert both "light" and "dark" were visited and it returned home).
    assert seen[0] == seen[-1]
    assert "light" in seen
    assert "dark" in seen


def test_theme_toggle_aria_label_matches_current_state(page):
    page.click("#theme-toggle-btn")
    page.wait_for_timeout(100)
    title = page.locator("#theme-toggle-btn").get_attribute("title")
    aria_label = page.locator("#theme-toggle-btn").get_attribute("aria-label")
    assert aria_label == title
    assert aria_label  # never empty — this is the icon-only button's only accessible name
