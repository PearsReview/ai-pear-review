"""Act Now: propose -> preview -> confirm/discard, through the coding-agent
path (app/services/harness_service.py). The app copy under test is pointed at
tests/fake_acp_agent.py's "comment" scenario (see conftest.py's app_server),
so the agent's edit is deterministic: it prepends one comment line to
sample.py. What's under test is the UI flow and the write guarantees, not
an agent's judgement."""

from __future__ import annotations

import time

# Starting the fake agent is a Python process launch plus a copy of the
# scratch repo — seconds, not a model call.
ACT_NOW_TIMEOUT_MS = 30_000
FAKE_AGENT_LINE = "# Reviewed with Act Now\n"


def _wait_for_act_now_outcome(page, timeout_ms: int = ACT_NOW_TIMEOUT_MS) -> str:
    """Returns "preview" or "error", whichever appears first."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if page.locator(".act-now-confirm-row:not(.hidden)").count() > 0:
            return "preview"
        if page.locator("#error-banner:not(.hidden)").count() > 0:
            return "error"
        page.wait_for_timeout(100)
    raise TimeoutError(f"neither a preview nor an error banner appeared within {timeout_ms}ms")


def _submit_act_now(page, instruction: str) -> str:
    # Act Now is one-shot: sendTextReply() deactivates it on send, so it's
    # clicked on for each request.
    page.locator("#act-now-btn:not([disabled])").wait_for(timeout=10_000)
    page.click("#act-now-btn")
    page.fill("#text-input", instruction)
    page.click("#send-btn")
    return _wait_for_act_now_outcome(page)


def test_act_now_preview_shows_a_diff_without_writing(page, scratch_repo, reset_scratch_repo):
    original_content = (scratch_repo / "sample.py").read_text(encoding="utf-8")

    assert _submit_act_now(page, "Add a comment at the top of the file.") == "preview"

    assert page.locator("#code-view .code-line").count() > 0
    assert "Added a comment" in page.locator("#act-now-summary").inner_text()
    # Nothing written to disk yet — the agent worked on a copy.
    assert (scratch_repo / "sample.py").read_text(encoding="utf-8") == original_content

    page.click("#act-now-discard-btn")
    page.wait_for_timeout(200)
    confirm_row_hidden = page.locator("#act-now-confirm-row").evaluate("el => el.classList.contains('hidden')")
    assert confirm_row_hidden is True
    assert (scratch_repo / "sample.py").read_text(encoding="utf-8") == original_content


def test_act_now_apply_button_stays_in_view_when_the_preview_scrolls(page, reset_scratch_repo):
    """The preview scrolls its change to the centre (renderCodeView), which for
    an edit far down a file used to carry the Apply/Discard bar out of view —
    it now sticks with the header (.code-pane-top). sample.py is too short to
    scroll, so padding below the code stands in for a long file, and scrolling
    to the bottom for an edit near its end."""
    assert _submit_act_now(page, "Add a comment at the top of the file.") == "preview"

    page.locator("#code-view").evaluate("el => { el.style.paddingBottom = '3000px'; }")
    pane = page.locator("#code-pane")
    pane.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    assert pane.evaluate("el => el.scrollTop") > 0
    page.wait_for_timeout(300)

    pane_box = pane.bounding_box()
    apply_box = page.locator("#act-now-apply-btn").bounding_box()
    assert pane_box and apply_box
    assert apply_box["y"] >= pane_box["y"]
    assert apply_box["y"] + apply_box["height"] <= pane_box["y"] + pane_box["height"]

    page.click("#act-now-discard-btn")


def test_act_now_confirm_writes_exactly_the_previewed_change(page, scratch_repo, reset_scratch_repo):
    original_bytes = (scratch_repo / "sample.py").read_bytes()
    assert _submit_act_now(page, "Add a comment at the top of the file.") == "preview"

    page.click("#act-now-apply-btn")
    page.wait_for_timeout(2000)
    confirm_row_hidden = page.locator("#act-now-confirm-row").evaluate("el => el.classList.contains('hidden')")
    assert confirm_row_hidden is True
    assert (scratch_repo / "sample.py").read_bytes() == FAKE_AGENT_LINE.encode() + original_bytes


def test_act_now_refine_replaces_the_preview_and_apply_writes_it(page, scratch_repo, reset_scratch_repo):
    """Refine re-runs the agent on top of the proposal on screen
    (handle_refine_act_now); the fake agent adds its line again, so the
    refined preview holds both, measured against the untouched repo."""
    original_bytes = (scratch_repo / "sample.py").read_bytes()
    assert _submit_act_now(page, "Add a comment at the top of the file.") == "preview"

    assert page.locator("#act-now-refine-btn").is_disabled()  # nothing typed yet
    page.fill("#act-now-refine-input", "Add it once more.")
    assert page.locator("#act-now-refine-btn").is_enabled()
    page.press("#act-now-refine-input", "Enter")

    page.wait_for_function(
        "() => document.querySelectorAll('#code-view .code-line.add').length === 2", timeout=ACT_NOW_TIMEOUT_MS
    )
    page.locator("#act-now-apply-btn:not([disabled])").wait_for(timeout=ACT_NOW_TIMEOUT_MS)
    assert page.locator("#act-now-refine-input").input_value() == ""
    assert (scratch_repo / "sample.py").read_bytes() == original_bytes  # still only a preview

    page.click("#act-now-apply-btn")
    page.wait_for_timeout(2000)
    assert (scratch_repo / "sample.py").read_bytes() == (FAKE_AGENT_LINE * 2).encode() + original_bytes


def test_act_now_shows_preset_chips_that_insert_without_sending(page):
    """Toggling Act Now on shows the preset chip row (PROMPT_SUGGESTIONS.actNow
    in static/js/interactions.js); clicking a chip inserts its text into #text-input
    without sending -- the reviewer can still edit/append before sending,
    same as any other prompt-suggestion chip (see insertPromptSuggestion).
    Purely a UI-state check, no agent involved."""
    page.locator("#act-now-btn:not([disabled])").wait_for(timeout=10_000)
    page.click("#act-now-btn")
    chips = page.locator("#prompt-suggestions-row .prompt-chip")
    chips.first.wait_for(state="visible", timeout=5000)
    assert chips.count() > 0

    first_chip_text = chips.first.inner_text()
    chips.first.click()
    assert page.locator("#text-input").input_value() == first_chip_text
    # Nothing sent yet -- no preview row, no error.
    assert page.locator(".act-now-confirm-row:not(.hidden)").count() == 0

    # Toggling Act Now back off swaps the row back to this context's normal
    # (non-Act-Now) suggestions -- a hunk is being presented here, so it
    # falls through to PROMPT_SUGGESTIONS.hunk rather than hiding, same as
    # it would if Act Now had never been toggled on.
    page.click("#act-now-btn")
    chips.first.wait_for(state="visible", timeout=5000)
    assert chips.first.inner_text() != first_chip_text


def test_act_now_chip_click_then_send_uses_the_normal_preview_flow(page, scratch_repo, reset_scratch_repo):
    """A chip-inserted instruction reaches the agent exactly like a hand-typed
    one (see sendTextReply/handle_act_now) — this only proves the chip path
    reaches the preview flow covered above."""
    page.locator("#act-now-btn:not([disabled])").wait_for(timeout=10_000)
    page.click("#act-now-btn")
    page.locator("#prompt-suggestions-row .prompt-chip").first.click()
    page.click("#send-btn")
    assert _wait_for_act_now_outcome(page) == "preview"

    page.click("#act-now-discard-btn")
