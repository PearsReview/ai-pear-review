"""The Explain button next to Next, and the "Explain changes" preference
that shows it (static/js/explain.js, prefs.js).

Driven with synthetic "presenting"/"narration" payloads through the __app
seam, like test_empty_states.py: a real narration needs a live model. The
server side — no model call on arrival, one on request — is covered in
tests/test_explain_on_request.py.
"""

from __future__ import annotations

import json

import pytest

HUNK = {
    "index": 0,
    "total": 1,
    "file_path": "sample.py",
    "header": "@@ -1,1 +1,1 @@",
    "diff": "",
    "full_lines": [{"type": "context", "old_no": 1, "new_no": 1, "text": "x = 1"}],
    "highlight_start": 0,
    "highlight_end": 0,
    "narration_available": True,
    "review_started": True,
    "review_ended": False,
    "narrated": False,
    "narrating": False,
    "done": False,
}


def _present(page, **overrides) -> None:
    page.evaluate(f"() => __app.onPresenting({json.dumps({**HUNK, **overrides})})")


def _explain(page):
    return page.locator("#explain-btn")


@pytest.fixture
def on_request(page):
    """Automatic explanations switched off the way a reviewer would."""
    page.evaluate("window.__sent = []; __app.sendHook = (type, payload) => __sent.push({type, payload});")
    page.click("#voice-settings-btn")
    page.check('input[name="narrate-pref"][value="request"]')
    return page


def test_hidden_while_changes_are_explained_automatically(page):
    # The shared fixtures pin "Automatically" (see conftest's
    # _suppress_auto_tour); a real first visit gets "When I ask".
    _present(page, narrating=True)
    assert _explain(page).is_hidden()


def test_on_request_is_the_default_for_a_first_visit(app_server, playwright):
    """A browser of its own: the shared fixtures seed "Automatically" on
    every navigation, which is exactly what this must not have."""
    browser = playwright.chromium.launch()
    try:
        fresh = browser.new_page(viewport={"width": 1280, "height": 900})
        fresh.add_init_script("try { localStorage.setItem('ai_pear_review_tour_seen', '1'); } catch (e) {}")
        fresh.goto(app_server, wait_until="load")
        fresh.wait_for_selector("#status-bar span", timeout=15000)
        assert fresh.is_checked('input[name="narrate-pref"][value="request"]')
        _present(fresh)
        assert _explain(fresh).is_visible()
    finally:
        browser.close()


def test_switching_the_preference_is_saved_and_sent(on_request):
    assert on_request.evaluate("localStorage.getItem('ai_pear_review_auto_narrate')") == "0"
    sent = on_request.evaluate("__sent.filter(m => m.type === 'set_narration_prefs')")
    assert sent == [{"type": "set_narration_prefs", "payload": {"auto_narrate": False}}]


def test_shown_next_to_next_for_an_unexplained_change(on_request):
    _present(on_request)
    assert _explain(on_request).is_visible() and _explain(on_request).is_enabled()
    # No "…thinking" placeholder: nothing is on its way.
    assert on_request.locator(".turn.thinking").count() == 0


def test_clicking_asks_for_this_change_and_waits(on_request):
    _present(on_request, index=0)
    _explain(on_request).click()
    sent = on_request.evaluate("__sent.filter(m => m.type === 'explain_hunk')")
    assert sent == [{"type": "explain_hunk", "payload": {"index": 0}}]
    assert _explain(on_request).is_disabled()  # one request at a time


def test_disabled_once_the_explanation_arrives(on_request):
    _present(on_request)
    _explain(on_request).click()
    on_request.evaluate(
        """() => __app.onNarration({index: 0, total: 1, file_path: "sample.py", text: "It sets x.",
            blocks: [], narration_available: true})"""
    )
    assert _explain(on_request).is_disabled()
    assert "Already explained" in _explain(on_request).get_attribute("title")


def test_a_revisited_change_that_was_explained_stays_disabled(on_request):
    _present(on_request, narrated=True, narrating=True)
    assert _explain(on_request).is_disabled()


def test_hidden_before_the_review_starts(on_request):
    _present(on_request, review_started=False)
    assert _explain(on_request).is_hidden()


def test_switching_back_to_automatic_hides_it(on_request):
    _present(on_request)
    on_request.check('input[name="narrate-pref"][value="auto"]')
    assert _explain(on_request).is_hidden()
