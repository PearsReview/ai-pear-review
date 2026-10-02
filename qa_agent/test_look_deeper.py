"""Look deeper: the button under an answer hands that question to the coding
agent, read-only, and the answer shows as its own turn with the model it ran
on. The app copy under test runs
tests/fake_acp_agent.py's "comment" scenario (see conftest.py's app_server),
which answers a read-only turn deterministically; the agent's safety policy
and cancellation are covered in tests/test_harness_service.py."""

from __future__ import annotations

# A Python process launch plus a local clone of the scratch repo — seconds.
LOOK_DEEPER_TIMEOUT_MS = 30_000
# A real narration from the suite's local model, which on CPU is tens of seconds.
NARRATION_TIMEOUT_MS = 120_000


def test_look_deeper_answers_as_its_own_turn_without_writing(page, scratch_repo, reset_scratch_repo):
    original = (scratch_repo / "sample.py").read_bytes()
    # The page fixture can yield on the "…thinking" placeholder, before the
    # local model's narration lands (tens of seconds on CPU) — wait for the
    # narration itself. Its button asks the default "look deeper at this
    # change" question.
    page.wait_for_selector("#transcript .turn.presenter", timeout=NARRATION_TIMEOUT_MS)
    button = page.locator(".turn.presenter .look-deeper-btn").first
    button.wait_for(timeout=5_000)
    page.wait_for_function("!document.querySelector('.turn.presenter .look-deeper-btn').disabled", timeout=10_000)
    button.click()

    turn = page.locator(".turn.deeper").first
    turn.wait_for(timeout=LOOK_DEEPER_TIMEOUT_MS)
    header = turn.locator(".role").inner_text()
    assert "Looked deeper" in header and "claude-sonnet-5" in header and "read-only" in header
    assert "nothing was changed" in turn.inner_text()
    assert "Checking sample.py first" not in turn.inner_text(), "the agent's step narration is dropped"

    assert "tested" not in turn.inner_text().lower(), "no per-answer model warning — that lives in settings"
    assert turn.locator(".look-deeper-btn").count() == 0, "an agent answer doesn't offer to look deeper at itself"
    assert (scratch_repo / "sample.py").read_bytes() == original


def test_settings_panel_names_the_agent_model_and_what_was_tested(page):
    page.click("#model-settings-btn")
    page.select_option("#setting-harness-agent", "cline")
    model = page.locator("#setting-harness-model")
    page.wait_for_function(
        "document.getElementById('setting-harness-model').textContent.includes('claude-sonnet-5')", timeout=10_000
    )
    assert "anthropic" in model.inner_text()
    assert "From your Cline settings" in page.locator("#setting-harness-model-source").inner_text()
    note = page.locator("#setting-harness-note").inner_text()
    assert "tested with claude-sonnet-5" in note and "Smaller models" in note
