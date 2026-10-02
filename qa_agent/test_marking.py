"""Double-click and keyboard line-marking (the "mark-gutter" dot), and the
2-line range chip — see static/js/interactions.js's toggleLineMark/markedLines."""

from __future__ import annotations


def test_double_click_marks_and_unmarks_a_line(page):
    line = page.locator(".code-line").first
    line.dblclick()
    page.wait_for_timeout(200)
    dot = line.locator(".mark-gutter")
    assert dot.evaluate("el => el.classList.contains('marked')") is True
    chip_visible = page.locator("#marked-lines-chip").evaluate("el => !el.classList.contains('hidden')")
    assert chip_visible is True

    line.dblclick()
    page.wait_for_timeout(200)
    assert dot.evaluate("el => el.classList.contains('marked')") is False
    chip_visible = page.locator("#marked-lines-chip").evaluate("el => el.classList.contains('hidden')")
    assert chip_visible is True  # "hidden" class present again


def test_mark_gutter_is_a_labeled_keyboard_reachable_button(page):
    dot = page.locator(".mark-gutter").first
    assert dot.evaluate("el => el.tagName") == "BUTTON"
    assert dot.get_attribute("aria-label")  # never unlabeled, icon-only control
    dot.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)
    assert dot.evaluate("el => el.classList.contains('marked')") is True
    # clean up so later tests in this module see a clean slate
    dot.click()


def test_two_line_range_marks_everything_between(page):
    lines = page.locator(".code-line")
    lines.nth(0).dblclick()
    page.wait_for_timeout(100)
    lines.nth(2).dblclick()
    page.wait_for_timeout(200)
    in_range_count = page.locator(".code-line.mark-range").count()
    # Inclusive of both endpoints and whatever's between them.
    assert in_range_count >= 3
    page.click("#clear-marks-btn")
    page.wait_for_timeout(150)
    assert page.locator(".code-line.mark-range").count() == 0
    chip_hidden = page.locator("#marked-lines-chip").evaluate("el => el.classList.contains('hidden')")
    assert chip_hidden is True
