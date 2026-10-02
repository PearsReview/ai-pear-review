"""Revisiting an already-presented hunk — via the file-explorer sidebar, or
via Next/Prev past it and back — must not duplicate the transcript turn or
restart the spoken narration.

Exercises both halves of the fix together, the way a real click does:
- app/handlers/narration.py's present_current_hunk `already_shown` gate (skips
  session.transcript.append and try_speak on a cache-hit revisit).
- static/js/transcript.js's onNarration `narratedByIndex` map (skips appendTurn when
  the server replays byte-identical text for a hunk index already shown).

The scratch repo (see conftest.py) has exactly one file and one hunk, which
auto-presents on connect — perfect for "revisit the same hunk" scenarios,
but not enough to exercise the File/Overall chat tabs' cross-file filtering;
that part is checked here only as far as "the current file's own turn stays
visible under the File chat tab," not "a different file's turn gets hidden."
"""

from __future__ import annotations

# Relocated to scenarios/scenario_helpers.py — test_messy_navigation.py's
# multi-hunk revisit scenario needs the identical counter, so it lives in
# the shared module now and this file imports it back rather than keeping
# two copies that could drift.
from .scenarios.scenario_helpers import tts_status_frame_count as _tts_status_frame_count


def test_reclicking_the_same_file_pill_does_not_duplicate_or_replay(page_with_ws):
    page, frames = page_with_ws

    # Wait for the auto-loaded first hunk's narration (and whatever TTS
    # attempt follows it) to fully land before touching anything, so
    # everything that follows is unambiguously caused by the re-click, not
    # a race with the initial load.
    frames.wait_for("narration", page, timeout_ms=30_000)
    page.wait_for_timeout(500)

    assert page.locator("#transcript .turn.presenter").count() == 1
    tts_frames_after_first_load = _tts_status_frame_count(frames)

    page.click("#file-sidebar-tab")  # expand the file explorer sidebar
    pill = page.locator(".file-pill").first
    pill.click()  # re-open the file that's already showing
    page.wait_for_timeout(500)
    pill.click()  # and again
    page.wait_for_timeout(500)

    assert page.locator("#transcript .turn.presenter").count() == 1, (
        "re-clicking an already-open file pill must not add a duplicate conversation turn"
    )
    assert _tts_status_frame_count(frames) == tts_frames_after_first_load, (
        "re-clicking an already-open file pill must not re-trigger TTS"
    )


def test_next_then_prev_back_to_the_same_hunk_does_not_duplicate_or_replay(page_with_ws):
    page, frames = page_with_ws
    frames.wait_for("narration", page, timeout_ms=30_000)
    page.wait_for_timeout(500)

    assert page.locator("#transcript .turn.presenter").count() == 1
    tts_frames_after_first_load = _tts_status_frame_count(frames)

    page.click("#next-btn")  # -> "All hunks reviewed" (only one hunk exists)
    page.wait_for_timeout(300)
    page.click("#prev-btn")  # -> back to the same hunk, a cache hit
    page.wait_for_timeout(500)

    assert page.locator("#transcript .turn.presenter").count() == 1, (
        "Prev back to an already-presented hunk must not add a duplicate conversation turn"
    )
    assert _tts_status_frame_count(frames) == tts_frames_after_first_load, (
        "Prev back to an already-presented hunk must not re-trigger TTS"
    )


def test_chat_tabs_toggle_and_file_chat_still_shows_current_file(page):
    page.wait_for_selector("#transcript .turn.presenter", timeout=15000)

    # Plain toggle-button-group state (see .chat-tabs/.chat-tab in
    # style.css and setDiffViewMode's identical Merged/Split pattern) —
    # .active is the only state marker, no tablist ARIA role/aria-selected.
    overall_tab = page.locator('.chat-tab[data-tab="overall"]')
    file_tab = page.locator('.chat-tab[data-tab="file"]')
    assert "active" in (overall_tab.get_attribute("class") or "")
    assert "active" not in (file_tab.get_attribute("class") or "")

    file_tab.click()
    assert "active" in (file_tab.get_attribute("class") or "")
    assert "active" not in (overall_tab.get_attribute("class") or "")
    # The one turn on screen belongs to the currently active file, so File
    # chat must still show it.
    turn = page.locator("#transcript .turn.presenter").first
    assert turn.evaluate("el => el.classList.contains('chat-hidden')") is False

    overall_tab.click()
    assert "active" in (overall_tab.get_attribute("class") or "")
    assert turn.evaluate("el => el.classList.contains('chat-hidden')") is False
