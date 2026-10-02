"""The model settings panel's TTS/STT sections — endpoint + token fields,
and provider-gated availability of context-size/max-reply-tokens.

Uses the shared `page` fixture from conftest.py (fresh browser per test,
already past "Start Review") rather than test_guided_tour.py's own
tour-seeded variant — nothing here cares about the tour.

Several tests here actually save settings, which persist server-side for
the rest of this session (app_server/scratch_repo are session-scoped, see
conftest.py's own note on this). Left unrestored, a save would leak into
every test that runs after it for the rest of the run — so each such test
restores the original value in a `finally` block, the same "don't let one
test's write outlive it" rule reset_scratch_repo enforces elsewhere in
this suite for the working tree itself.
"""

from __future__ import annotations

import json


def test_settings_panel_has_a_tts_endpoint_field_that_round_trips(page):
    """Wire-level check that the setting actually reaches the panel and
    can be changed — sanitize_tts/effective_tts_settings themselves are
    covered at the unit level (tests/test_settings_store_tts.py).

    handle_set_settings applies a tts change immediately — it live-
    reconstructs the module-global TTS client, not per-connection like the
    conversation client."""
    page.click("#model-settings-btn")
    # The field exists in the DOM (and is visible) the instant the panel
    # opens — its VALUE only arrives once the get_settings round trip
    # completes, a beat later. Waiting for the element alone would race
    # that response; this waits for what actually matters.
    page.wait_for_function("document.getElementById('setting-tts-endpoint').value !== ''", timeout=10000)
    original = page.locator("#setting-tts-endpoint").input_value()
    assert original, "config.yaml always sets a default tts.endpoint"

    try:
        page.fill("#setting-tts-endpoint", "http://example.invalid:9000/speech")
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('TTS')", timeout=10000)
        assert "reload" not in page.locator("#setting-status").inner_text().lower(), (
            "tts applies immediately, unlike the conversation settings — must not claim a reload is needed"
        )

        # Round trip: close and reopen re-fetches rather than caching (see
        # openModelSettingsPanel), so this confirms the new value was
        # actually persisted server-side, not just echoed in the save ack.
        page.click("#model-settings-btn")
        page.click("#model-settings-btn")
        page.wait_for_function(
            "document.getElementById('setting-tts-endpoint').value === 'http://example.invalid:9000/speech'",
            timeout=10000,
        )
    finally:
        page.fill("#setting-tts-endpoint", original)
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('TTS')", timeout=10000)


def test_settings_panel_has_an_stt_endpoint_field_that_round_trips(page):
    """STT's mirror of the TTS round-trip test above — same wire path,
    same live-reconstruction (global STT), same reasoning."""
    page.click("#model-settings-btn")
    page.wait_for_function("document.getElementById('setting-stt-endpoint').value !== ''", timeout=10000)
    original = page.locator("#setting-stt-endpoint").input_value()
    assert original, "config.yaml always sets a default stt.endpoint"

    try:
        page.fill("#setting-stt-endpoint", "http://example.invalid:9000/transcribe")
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('STT')", timeout=10000)
        assert "reload" not in page.locator("#setting-status").inner_text().lower(), (
            "stt applies immediately, unlike the conversation settings — must not claim a reload is needed"
        )

        page.click("#model-settings-btn")
        page.click("#model-settings-btn")
        page.wait_for_function(
            "document.getElementById('setting-stt-endpoint').value === 'http://example.invalid:9000/transcribe'",
            timeout=10000,
        )
    finally:
        page.fill("#setting-stt-endpoint", original)
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('STT')", timeout=10000)


def test_tts_token_field_never_echoes_the_saved_secret_back(page):
    """A saved token must never round-trip back to the client (see
    effective_tts_settings, which reports only token_set) — the field
    stays blank across a close/reopen, and only the placeholder changes to
    say a token is saved.

    Cleanup goes around the UI on purpose: the Save button only ever
    includes a token in the outgoing payload when the field is non-empty
    (see settingSaveBtn's click handler and sanitize_tts's own docstring —
    a blank field means "leave it alone", by design, so there's no UI path
    to clear one). Sending set_settings directly with token: "" reaches
    the same server-side sanitize_tts allowlist a real client-typed empty
    string would if the UI ever grew a "clear" affordance — it's a
    deliberate escape hatch for this test, not a route around any check.
    """
    page.click("#model-settings-btn")
    page.wait_for_function("document.getElementById('setting-tts-endpoint').value !== ''", timeout=10000)

    try:
        page.fill("#setting-tts-token", "test-secret-token")
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('TTS')", timeout=10000)

        page.click("#model-settings-btn")
        page.click("#model-settings-btn")
        page.wait_for_function(
            "document.getElementById('setting-tts-token').placeholder.includes('saved')",
            timeout=10000,
        )
        assert page.locator("#setting-tts-token").input_value() == "", (
            "the real secret must never be pre-filled back into the field"
        )
    finally:
        page.evaluate(
            "() => { __app.send('set_settings', {settings: {tts: {token: ''}}}); __app.send('get_settings', {}); }"
        )
        page.wait_for_function(
            "document.getElementById('setting-tts-token').placeholder === ''",
            timeout=10000,
        )


def test_stt_token_field_never_echoes_the_saved_secret_back(page):
    """STT's mirror of the TTS no-echo test above."""
    page.click("#model-settings-btn")
    page.wait_for_function("document.getElementById('setting-stt-endpoint').value !== ''", timeout=10000)

    try:
        page.fill("#setting-stt-token", "test-secret-token")
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('STT')", timeout=10000)

        page.click("#model-settings-btn")
        page.click("#model-settings-btn")
        page.wait_for_function(
            "document.getElementById('setting-stt-token').placeholder.includes('saved')",
            timeout=10000,
        )
        assert page.locator("#setting-stt-token").input_value() == "", (
            "the real secret must never be pre-filled back into the field"
        )
    finally:
        page.evaluate(
            "() => { __app.send('set_settings', {settings: {stt: {token: ''}}}); __app.send('get_settings', {}); }"
        )
        page.wait_for_function(
            "document.getElementById('setting-stt-token').placeholder === ''",
            timeout=10000,
        )


def test_saving_tts_endpoint_alone_does_not_wipe_a_previously_saved_token(page):
    """Regression pin for a real bug fixed alongside adding the token
    field: apply_overrides used to replace the stored "tts" override
    wholesale, so saving just a new endpoint would silently drop a
    previously-saved token from disk (even though the in-memory CONFIG
    update was already a correct merge). It now merges one level deep,
    same as the ollama/anthropic provider sections — see
    tests/test_settings_store_tts.py for the unit-level pin of the same
    fix; this is the same behaviour exercised end-to-end through the real
    save path."""
    page.click("#model-settings-btn")
    page.wait_for_function("document.getElementById('setting-tts-endpoint').value !== ''", timeout=10000)
    original_endpoint = page.locator("#setting-tts-endpoint").input_value()

    try:
        page.fill("#setting-tts-token", "keep-me-token")
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('TTS')", timeout=10000)

        # Endpoint-only save: the token field is left blank.
        page.fill("#setting-tts-endpoint", "http://example.invalid:9000/speech")
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('TTS')", timeout=10000)

        page.click("#model-settings-btn")
        page.click("#model-settings-btn")
        page.wait_for_function(
            "document.getElementById('setting-tts-endpoint').value === 'http://example.invalid:9000/speech'",
            timeout=10000,
        )
        assert "saved" in page.locator("#setting-tts-token").get_attribute("placeholder"), (
            "the token saved earlier must still be in force after an endpoint-only save"
        )
    finally:
        page.evaluate(
            "(endpoint) => { __app.send('set_settings', {settings: {tts: {endpoint, token: ''}}}); "
            "__app.send('get_settings', {}); }",
            original_endpoint,
        )
        page.wait_for_function(
            f"document.getElementById('setting-tts-endpoint').value === {json.dumps(original_endpoint)} "
            "&& document.getElementById('setting-tts-token').placeholder === ''",
            timeout=10000,
        )


def test_switching_provider_actually_saves(page):
    """The test below deliberately stops before saving, and that gap hid a
    real bug: onSettings snapshotted the provider showing in the DROPDOWN,
    which with suppressProviderSync set is the reviewer's unsaved pick. Save
    then compared that pick against itself, decided nothing had changed, and
    never sent "provider" — so the ack came back still saying ollama and the
    dropdown reverted a beat after Save. Switching to Anthropic from the
    panel was impossible, with no error to show for it.

    Checks the round trip (close/reopen re-fetches), not just the ack, so an
    echo of what was sent can't pass this."""
    page.click("#model-settings-btn")
    page.wait_for_function("document.getElementById('setting-timeout').value !== ''", timeout=10000)
    original = page.locator("#setting-provider").input_value()
    assert original == "ollama", "this suite's config.yaml defaults to ollama"

    try:
        page.select_option("#setting-provider", "anthropic")
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('reload')", timeout=10000)
        assert page.locator("#setting-provider").input_value() == "anthropic", (
            "the save ack must not revert the dropdown — that was the original symptom"
        )

        page.click("#model-settings-btn")
        page.click("#model-settings-btn")
        page.wait_for_function("document.getElementById('setting-provider').value === 'anthropic'", timeout=10000)
    finally:
        page.select_option("#setting-provider", original)
        page.click("#setting-save-btn")
        page.wait_for_selector("#setting-status:has-text('reload')", timeout=10000)


def test_model_choices_follow_the_provider_you_picked(page):
    """The model control is a <select>, and its options come from the
    provider showing in the panel — not the saved one. get_settings used to
    carry no provider at all, so picking Anthropic left Ollama's installed
    models in the list: every option was a local model and no Claude model
    could be chosen.

    Runs without an ANTHROPIC_API_KEY: with no key the server lists nothing,
    and the saved model is offered alone — which is precisely the assertion
    that fails if the list is still Ollama's."""
    page.click("#model-settings-btn")
    page.wait_for_function("document.getElementById('setting-timeout').value !== ''", timeout=10000)
    ollama_model = page.locator("#setting-model").input_value()
    assert ollama_model.startswith("qwen"), "this suite's config.yaml pins an ollama qwen model"

    page.select_option("#setting-provider", "anthropic")
    page.wait_for_function("document.getElementById('setting-model').value.startsWith('claude')", timeout=10000)
    options = page.locator("#setting-model option").all_text_contents()
    assert ollama_model not in options, f"Anthropic's model list still contains the ollama model: {options}"


def test_save_is_blocked_until_the_new_provider_s_models_arrive(page):
    """Saving in the gap between picking a provider and its models arriving
    wrote the OLD provider's model under the NEW provider's key — an ollama
    model id stored as conversation.anthropic.model, which surfaces only as
    a 404 from the Messages API much later. This repo's own
    .review/ui_settings.json was corrupted exactly that way."""
    page.click("#model-settings-btn")
    page.wait_for_function("document.getElementById('setting-timeout').value !== ''", timeout=10000)
    assert not page.locator("#setting-save-btn").is_disabled()

    page.select_option("#setting-provider", "anthropic")
    # Re-enabled by the response, so this asserts the gap exists at all —
    # without the guard the button is never disabled and this fails here.
    page.wait_for_function("document.getElementById('setting-save-btn').disabled === true", timeout=2000)
    page.wait_for_function("document.getElementById('setting-save-btn').disabled === false", timeout=10000)


def test_anthropic_hides_context_size_but_keeps_max_reply_tokens(page):
    """Context size is an Ollama concept — Anthropic manages its own
    window, so the row is hidden outright rather than merely disabled: a
    greyed-out-but-visible box reads as "you can't touch this right now",
    which is wrong when the setting doesn't apply at all.

    Max reply tokens is the opposite, and used to be hidden here too on the
    grounds that Anthropic should keep config.yaml's default. That default
    is 300 — sized for a local model's spoken turn, and on a Claude model
    shared with its own reasoning, so it truncated replies mid-sentence and
    sometimes returned no text at all. It has to be reachable.

    Read-only check — selecting a provider alone never saves anything, so
    nothing needs restoring."""
    page.click("#model-settings-btn")
    # Wait for the FIRST get_settings response specifically, not just for
    # #setting-provider to have a value — a <select> already has one (its
    # first <option>) before any JS runs at all, so that check passes
    # instantly and doesn't prove the round trip happened. #setting-timeout
    # is a number input that starts genuinely empty, so waiting on it
    # actually pins the response down — without this, selecting "anthropic"
    # a moment later can race that still-in-flight first response (its
    # installed-models lookup has variable latency) and get clobbered by
    # it arriving second with the pre-selection "ollama" state.
    page.wait_for_function("document.getElementById('setting-timeout').value !== ''", timeout=10000)

    page.select_option("#setting-provider", "anthropic")
    page.wait_for_function("document.getElementById('setting-num-ctx-row').classList.contains('hidden')", timeout=10000)
    assert not page.locator("#setting-num-ctx-row").is_visible()
    assert page.locator("#setting-num-ctx").is_disabled(), "hidden AND disabled — belt and braces"
    assert page.locator("#setting-max-tokens-row").is_visible(), "Anthropic needs its own reply budget"
    assert not page.locator("#setting-max-tokens").is_disabled()

    page.select_option("#setting-provider", "ollama")
    page.wait_for_function(
        "!document.getElementById('setting-num-ctx-row').classList.contains('hidden')", timeout=10000
    )
    assert page.locator("#setting-num-ctx-row").is_visible()
    assert page.locator("#setting-max-tokens-row").is_visible()
    assert not page.locator("#setting-num-ctx").is_disabled()
    assert not page.locator("#setting-max-tokens").is_disabled()
