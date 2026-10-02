"""The guided tour (static/js/tour.js) — pure DOM/state
behaviour, no LLM involved, so this runs fast and asserts hard rather than
recording findings.

Every other fixture in this suite (page/page_with_ws, and the generated
suite's own page fixture) now pre-seeds the tour's "seen" flag via
conftest.py's _suppress_auto_tour — without that, a fresh browser with
empty localStorage is exactly the condition the tour's own auto-start
logic looks for, and it would fire mid-test across the whole regression
suite. This file is the one place that flag is deliberately NOT always
pre-seeded, since proving the auto-start behaviour actually works is the
point of test_auto_start_fires_once_per_browser below.
"""

from __future__ import annotations

import pytest


def _open_page(playwright, app_server: str, *, suppress_auto_tour: bool):
    browser = playwright.chromium.launch()
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    if suppress_auto_tour:
        pg.add_init_script("try { localStorage.setItem('ai_pear_review_tour_seen', '1'); } catch (e) {}")
    pg.goto(app_server, wait_until="load")
    pg.wait_for_selector("#status-bar span", timeout=15000)
    return browser, pg


@pytest.fixture
def page(app_server: str, playwright):
    """A page with the tour pre-seeded as seen, same as the shared suite's
    own `page` fixture — every test here except the auto-start one wants a
    clean slate that won't have the tour competing for the screen."""
    browser, pg = _open_page(playwright, app_server, suppress_auto_tour=True)
    yield pg
    browser.close()


def _progress_text(pg) -> str:
    return pg.locator("#tour-card-progress").inner_text()


def test_tour_button_opens_and_closes_manually(page):
    assert page.locator("#tour-card").is_hidden()
    page.click("#tour-btn")
    assert page.locator("#tour-card").is_visible()
    assert "Step 1 of" in _progress_text(page)
    page.click("#tour-skip-btn")
    assert page.locator("#tour-card").is_hidden()
    assert page.locator("#tour-highlight").is_hidden()


def test_next_and_back_navigate_steps(page):
    page.click("#tour-btn")
    assert "Step 1 of" in _progress_text(page)
    page.click("#tour-next-btn")
    assert "Step 2 of" in _progress_text(page)
    page.click("#tour-back-btn")
    assert "Step 1 of" in _progress_text(page)


def test_back_is_disabled_on_the_first_step(page):
    """The first step has no target and always exists, so Back searching
    backward from it must find nothing — disabled, not a dead click."""
    page.click("#tour-btn")
    assert page.locator("#tour-back-btn").is_disabled()


def test_escape_closes_the_tour(page):
    page.click("#tour-btn")
    page.keyboard.press("Escape")
    assert page.locator("#tour-card").is_hidden()


def test_arrow_keys_navigate_like_the_buttons(page):
    page.click("#tour-btn")
    page.keyboard.press("ArrowRight")
    assert "Step 2 of" in _progress_text(page)
    page.keyboard.press("ArrowLeft")
    assert "Step 1 of" in _progress_text(page)


def test_reopening_via_the_button_toggles_rather_than_restarts(page):
    """The ? button is a single toggle — clicking it while the tour is
    already open should close it, not reset to step 1 silently."""
    page.click("#tour-btn")
    page.click("#tour-next-btn")
    assert "Step 2 of" in _progress_text(page)
    page.click("#tour-btn")
    assert page.locator("#tour-card").is_hidden()


def test_the_last_step_says_done_and_closes_on_click(page):
    page.click("#tour-btn")
    # The VISIBLE step count, not TOUR_STEPS.length — some steps' targets
    # (Finish Review, most reliably) don't exist for a fresh session with
    # nothing queued, so the raw array length overshoots how many clicks
    # this walk actually needs.
    total = page.evaluate("__app.tourVisibleStepIndices().length")
    for _ in range(total - 1):
        page.click("#tour-next-btn")
    assert page.locator("#tour-next-btn").inner_text().strip() == "Done"
    page.click("#tour-next-btn")
    assert page.locator("#tour-card").is_hidden()


def test_highlight_overlaps_the_targeted_element(page):
    """Sanity check on the positioning math: advance to a step with a
    known-always-visible target and confirm the highlight box actually
    covers it, rather than trusting the arithmetic by inspection."""
    page.click("#tour-btn")
    page.click("#tour-next-btn")  # step 2: #start-review-btn, if visible...
    # ...but that one may already be past (review auto-started by an
    # earlier test's own state) — walk forward to a step guaranteed
    # present instead: "Moving through the diff" targets .toolbar .controls,
    # which always exists regardless of review state.
    for _ in range(6):
        if "Moving through the diff" in page.locator("#tour-card-title").inner_text():
            break
        page.click("#tour-next-btn")
    else:
        pytest.fail("could not reach the .toolbar .controls step")

    # .tour-highlight's position/size transition over 200ms (see
    # style.css) — a real, deliberate bit of motion polish, but it means a
    # bounding_box() read immediately after the click can land mid-animation
    # and not yet match the target it's settling toward. Found by this
    # exact test failing on the width/right-edge comparison below while the
    # top/left ones passed — consistent with a transition still in flight,
    # not a positioning-math bug.
    page.wait_for_timeout(250)
    target_box = page.locator(".toolbar .controls").bounding_box()
    highlight_box = page.locator("#tour-highlight").bounding_box()
    # The highlight pads 6px around the target on every side (see
    # positionTourHighlight) — assert containment with that slack, not
    # exact equality, since exact pixel matching is what changes if the
    # padding constant ever does.
    assert highlight_box["x"] <= target_box["x"]
    assert highlight_box["y"] <= target_box["y"]
    assert highlight_box["x"] + highlight_box["width"] >= target_box["x"] + target_box["width"]
    assert highlight_box["y"] + highlight_box["height"] >= target_box["y"] + target_box["height"]


def test_file_explorer_step_opens_and_then_restores_the_sidebar(page):
    """The file explorer starts collapsed by default. The tour step that
    talks about it must reveal it to point at something real — and put it
    back afterward, rather than leaving the reviewer's own collapsed
    preference changed just because they looked at the tour."""
    assert "collapsed" in (page.locator("#file-sidebar").get_attribute("class") or "")

    page.click("#tour-btn")
    for _ in range(6):
        if "File explorer" in page.locator("#tour-card-title").inner_text():
            break
        page.click("#tour-next-btn")
    else:
        pytest.fail("could not reach the file explorer step")

    assert "collapsed" not in (page.locator("#file-sidebar").get_attribute("class") or "")

    page.click("#tour-next-btn")  # leave the step
    assert "collapsed" in (page.locator("#file-sidebar").get_attribute("class") or ""), (
        "the sidebar's own collapsed-by-default state must be restored on leaving the step"
    )


def test_auto_start_fires_once_per_browser_when_there_is_something_to_review(app_server: str, playwright):
    """The one test in this file that deliberately does NOT pre-seed the
    tour-seen flag — everything else here suppresses it exactly so this
    behaviour doesn't fire unexpectedly mid-test elsewhere."""
    browser, pg = _open_page(playwright, app_server, suppress_auto_tour=False)
    try:
        assert pg.locator("#tour-card").is_hidden(), "must not appear before review_progress establishes a total"
        # maybeAutoStartTour fires 600ms after review_progress; give it
        # real margin above that rather than a value that could flake.
        pg.wait_for_selector("#tour-card:not(.hidden)", timeout=5000)
        assert "Step 1 of" in pg.locator("#tour-card-progress").inner_text()

        pg.click("#tour-skip-btn")
        assert pg.evaluate("localStorage.getItem('ai_pear_review_tour_seen')") == "1"
    finally:
        browser.close()


def test_auto_start_does_not_fire_a_second_time(app_server: str, playwright):
    browser = playwright.chromium.launch()
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    pg.add_init_script("try { localStorage.setItem('ai_pear_review_tour_seen', '1'); } catch (e) {}")
    try:
        pg.goto(app_server, wait_until="load")
        pg.wait_for_selector("#status-bar span", timeout=15000)
        pg.wait_for_timeout(1200)  # past the 600ms auto-start delay
        assert pg.locator("#tour-card").is_hidden()
    finally:
        browser.close()


# --- Read-aloud (the app's own TTS service, via speak_text/tour_audio_chunk) ---
#
# Real synthesis needs a live STT/TTS microservice this test environment
# doesn't run, so these work at the message/state level: stubbing send()
# to record what's actually sent rather than round-tripping it, and
# setting ttsAvailable directly (the same live variable the header's TTS
# status pill already reads from service_status — see updateStatus)
# instead of depending on a real service being up or down. The unit-level
# sanitize_tts()/effective_tts_settings() coverage lives in
# tests/test_settings_store_tts.py; this file is the wire-level/UI half.


def _stub_send(page) -> None:
    """Diverts the page's own send() into a recorder via the __app.sendHook
    seam — real synthesis needs a live TTS service this environment doesn't
    have, and this also lets tests read exactly what payload a click
    produced rather than inferring it from a side effect. The hook swallows
    the message rather than forwarding it, so nothing reaches the server."""
    page.evaluate(
        """() => {
            window.__sendCalls = [];
            window.__app.sendHook = (type, payload) => { window.__sendCalls.push({type, payload: payload || {}}); };
        }"""
    )


def _sent(page, msg_type: str):
    return page.evaluate(f"window.__sendCalls.filter(c => c.type === {msg_type!r})")


def _set_tts_available(page, available: bool) -> None:
    page.evaluate(f"__app.state.ttsAvailable = {'true' if available else 'false'}")


def test_speak_button_is_off_by_default(page):
    assert page.locator("#tour-speak-btn").get_attribute("aria-pressed") == "false"


def test_speak_button_toggles_and_persists_when_tts_is_available(page):
    page.click("#tour-btn")
    _set_tts_available(page, True)
    page.click("#tour-speak-btn")
    assert page.locator("#tour-speak-btn").get_attribute("aria-pressed") == "true"
    assert page.evaluate("localStorage.getItem('ai_pear_review_tour_speech_on')") == "1"

    page.click("#tour-speak-btn")
    assert page.locator("#tour-speak-btn").get_attribute("aria-pressed") == "false"
    assert page.evaluate("localStorage.getItem('ai_pear_review_tour_speech_on')") == "0"


def test_clicking_speak_when_tts_unavailable_opens_settings_instead_of_toggling(page):
    """The user's own ask: if the TTS model isn't set up, don't play
    audio — and pop up settings so it can be set up, rather than the
    button silently doing nothing."""
    page.click("#tour-btn")
    _set_tts_available(page, False)
    assert page.locator("#model-settings-panel").is_hidden()

    page.click("#tour-speak-btn")

    assert page.locator("#tour-speak-btn").get_attribute("aria-pressed") == "false", (
        "nothing was actually enabled — the button must not claim it was"
    )
    assert page.locator("#model-settings-panel").is_visible()


def test_the_toggle_turns_itself_off_when_tts_is_found_to_be_down(page):
    """The gap a naive implementation leaves: turning the toggle on while
    TTS is still optimistically believed available, then having the real
    attempt fail, must not leave the button claiming "on" forever after —
    found by driving this exact sequence against the real (unreachable in
    this environment) TTS endpoint and watching the button stay pressed
    after a real failure. updateStatus calls onTourTtsUnavailable on every
    service_status carrying tts: false; simulated here directly rather
    than waiting out a real ~4s connection-refused timeout."""
    page.click("#tour-btn")
    _set_tts_available(page, True)
    page.click("#tour-speak-btn")
    assert page.locator("#tour-speak-btn").get_attribute("aria-pressed") == "true"

    page.evaluate("__app.updateStatus({tts: false})")

    assert page.locator("#tour-speak-btn").get_attribute("aria-pressed") == "false"
    assert page.evaluate("localStorage.getItem('ai_pear_review_tour_speech_on')") == "0"


def test_advancing_a_step_sends_speak_text_only_when_enabled_and_available(page):
    page.click("#tour-btn")
    _set_tts_available(page, True)
    _stub_send(page)

    page.click("#tour-next-btn")
    assert _sent(page, "speak_text") == [], "must not speak while the toggle is off"

    page.click("#tour-speak-btn")  # turns on; speaks the CURRENT step immediately
    sent = _sent(page, "speak_text")
    assert len(sent) == 1
    title = page.locator("#tour-card-title").inner_text()
    body = page.locator("#tour-card-body").inner_text()
    spoken_text = sent[0]["payload"]["text"]
    assert title in spoken_text and body in spoken_text

    page.click("#tour-next-btn")
    assert len(_sent(page, "speak_text")) == 2, "advancing while on must speak the new step too"


def test_advancing_a_step_never_sends_speak_text_when_tts_unavailable(page):
    """Even with the toggle already on (from an earlier, working session —
    tourSpeechOn persists across page loads), a step change must not keep
    trying once the service is known to be down."""
    page.click("#tour-btn")
    _set_tts_available(page, True)
    page.click("#tour-speak-btn")  # on
    _set_tts_available(page, False)  # service went down
    _stub_send(page)

    page.click("#tour-next-btn")
    assert _sent(page, "speak_text") == []


def test_turning_speech_off_mid_playback_stops_immediately(page):
    page.click("#tour-btn")
    _set_tts_available(page, True)
    page.click("#tour-speak-btn")  # on
    page.evaluate(
        "__app.tourAudio.queue.push({mime_type:'audio/wav', audio_base64:'AA=='}); __app.tourAudio.playing = true;"
    )

    page.click("#tour-speak-btn")  # off, mid-"playback"
    assert page.evaluate("__app.tourAudio.queue.length") == 0
    assert page.evaluate("__app.tourAudio.playing") is False


def test_closing_the_tour_stops_tour_audio(page):
    page.click("#tour-btn")
    _set_tts_available(page, True)
    page.click("#tour-speak-btn")
    page.evaluate(
        "__app.tourAudio.queue.push({mime_type:'audio/wav', audio_base64:'AA=='}); __app.tourAudio.playing = true;"
    )

    page.click("#tour-skip-btn")
    assert page.evaluate("__app.tourAudio.queue.length") == 0
    assert page.evaluate("__app.tourAudio.playing") is False
