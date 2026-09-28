"""Inline GitHub-PR-style review comments: hover "+" -> composer -> queued
badge -> expand/edit/remove -> Finish Review. See static/js/comments.js's
openComposerForLine/renderQueuedCommentCardHtml and the batched-review
plan this feature was built from."""

from __future__ import annotations


def _open_composer(page, line_index: int = 0):
    line = page.locator(".code-line").nth(line_index)
    line.hover()
    line.locator(".line-comment-add").click()
    page.wait_for_timeout(150)


def _queue_one_comment(page, text: str, line_index: int = 0):
    # Each test's `page` fixture opens a fresh WebSocket connection, which
    # gets a brand-new server-side Session (see conftest.py's module
    # docstring) — nothing queued by an earlier test still exists, so any
    # test needing an existing badge to interact with has to queue its
    # own first rather than assuming one carried over.
    _open_composer(page, line_index)
    page.fill(".line-comment-composer-textarea", text)
    page.click(".line-comment-send-btn")
    page.wait_for_timeout(400)


def test_request_change_checkbox_no_longer_exists(page):
    # Fully replaced by the inline composer this session — regression
    # guard against it reappearing.
    assert page.locator("#request-change-checkbox").count() == 0


def test_add_button_hidden_until_hover(page):
    line = page.locator(".code-line").first
    add_btn = line.locator(".line-comment-add")
    assert add_btn.evaluate("el => getComputedStyle(el).opacity") == "0"
    line.hover()
    page.wait_for_timeout(100)
    assert add_btn.evaluate("el => getComputedStyle(el).opacity") == "1"


def test_composer_submit_creates_a_badge(page):
    _open_composer(page)
    page.fill(".line-comment-composer-textarea", "qa_agent: composer submit check.")
    page.click(".line-comment-send-btn")
    page.wait_for_timeout(400)
    assert page.locator(".line-comment-composer").count() == 0
    assert page.locator(".line-comment-badge").count() == 1
    assert page.locator("#review-queue-count").inner_text().strip() == "1 comment"


def test_escape_cancels_composer_without_queuing(page):
    before = page.locator(".line-comment-badge").count()
    _open_composer(page, line_index=1)
    page.fill(".line-comment-composer-textarea", "should not be sent")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    assert page.locator(".line-comment-composer").count() == 0
    assert page.locator(".line-comment-badge").count() == before


def test_ctrl_enter_submits_composer(page):
    before = page.locator(".line-comment-badge").count()
    _open_composer(page, line_index=1)
    page.fill(".line-comment-composer-textarea", "qa_agent: ctrl+enter submit check.")
    page.keyboard.press("Control+Enter")
    page.wait_for_timeout(400)
    assert page.locator(".line-comment-badge").count() == before + 1


def test_badge_expand_and_collapse(page):
    _queue_one_comment(page, "qa_agent: expand/collapse check.")
    badge = page.locator(".line-comment-badge").first
    assert badge.get_attribute("aria-expanded") == "false"
    badge.click()
    page.wait_for_timeout(150)
    assert badge.get_attribute("aria-expanded") == "true"
    body_hidden = page.locator(".line-comment-card-body").first.evaluate("el => el.classList.contains('hidden')")
    assert body_hidden is False
    badge.click()
    page.wait_for_timeout(150)
    assert badge.get_attribute("aria-expanded") == "false"


def test_edit_updates_the_queued_instruction(page):
    _queue_one_comment(page, "qa_agent: original instruction.")
    page.locator(".line-comment-badge").first.click()
    page.wait_for_timeout(150)
    page.click('[data-action="edit"]')
    page.wait_for_timeout(150)
    page.fill(".line-comment-edit-textarea", "qa_agent: edited instruction.")
    page.click('[data-action="done"]')
    page.wait_for_timeout(300)
    body_text = page.locator(".line-comment-card-body .comment-body").first.inner_text()
    assert body_text == "qa_agent: edited instruction."


def test_remove_requires_confirmation_and_reads_as_destructive(page):
    _queue_one_comment(page, "qa_agent: remove check.")
    page.locator(".line-comment-badge").first.click()  # expand — edit/remove only exist inside the expanded card body
    page.wait_for_timeout(150)
    remove_btn = page.locator('[data-action="remove"]').first
    edit_btn = page.locator('[data-action="edit"]').first
    remove_color = remove_btn.evaluate("el => getComputedStyle(el).color")
    edit_color = edit_btn.evaluate("el => getComputedStyle(el).color")
    assert remove_color != edit_color

    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept("")))
    badge_count_before = page.locator(".line-comment-badge").count()
    remove_btn.click()
    page.wait_for_timeout(300)
    assert len(dialogs) == 1 and "remove" in dialogs[0].lower()
    assert page.locator(".line-comment-badge").count() == badge_count_before - 1


def test_composer_defaults_to_suggestion_severity(page):
    _open_composer(page)
    select = page.locator(".line-comment-severity-select")
    assert select.input_value() == "suggestion"


def test_selected_severity_shows_on_the_badge_and_expanded_card(page):
    _open_composer(page, line_index=1)
    page.select_option(".line-comment-severity-select", "must-fix")
    page.fill(".line-comment-composer-textarea", "qa_agent: severity check.")
    page.click(".line-comment-send-btn")
    page.wait_for_timeout(400)

    badge = page.locator(".line-comment-badge").first
    assert "severity-must-fix" in (badge.get_attribute("class") or "")
    badge.click()
    page.wait_for_timeout(150)
    tag = page.locator(".line-comment-card-body .severity-tag").first
    assert tag.inner_text().strip().lower() == "must fix"


def test_editing_a_comment_can_change_its_severity(page):
    _queue_one_comment(page, "qa_agent: severity edit check.")
    badge = page.locator(".line-comment-badge").first
    badge.click()
    page.wait_for_timeout(150)
    page.click('[data-action="edit"]')
    page.wait_for_timeout(150)
    page.select_option(".line-comment-edit-severity-select", "nit")
    page.click('[data-action="done"]')
    page.wait_for_timeout(300)
    assert "severity-nit" in (badge.get_attribute("class") or "")
    tag = page.locator(".line-comment-card-body .severity-tag").first
    assert tag.inner_text().strip().lower() == "nit"


def test_create_plan_asks_first_and_clears_the_queue(page):
    _open_composer(page, line_index=0)
    page.fill(".line-comment-composer-textarea", "qa_agent: one to finish.")
    page.click(".line-comment-send-btn")
    page.wait_for_timeout(400)
    assert page.locator(".line-comment-badge").count() >= 1

    # Create plan opens the hand-off dialog; nothing is sent until it's confirmed.
    page.click("#finish-review-btn")
    page.wait_for_selector("#handoff-dialog[open]", timeout=3000)
    assert page.locator(".line-comment-badge").count() >= 1
    page.click("#handoff-create-btn")
    page.wait_for_timeout(600)
    assert page.locator("#handoff-dialog[open]").count() == 0
    assert page.locator(".line-comment-badge").count() == 0
    queue_hidden = page.locator("#review-queue-row").evaluate("el => el.classList.contains('hidden')")
    assert queue_hidden is True
