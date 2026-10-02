"""Markdown preview: block rendering, block selection, view-mode isolation,
and the read-along highlight's weight math.

Driven with synthetic payloads via page.evaluate (the same approach
test_empty_states.py takes) rather than a real .md file in the scratch
repo: adding one would put a second hunk in the seeded diff and break
test_navigation.py's single-hunk assumption. This targets the risky part
anyway — the client rendering and state isolation — and needs no TTS
service running, which the highlight path would otherwise depend on.
"""

from __future__ import annotations

import base64
import struct

PREVIEW_PAYLOAD = """() => __app.onMdPreview({
  file_path: "notes.md",
  content_hash: "deadbeefdeadbeef",
  blocks: [
    {kind: "heading", start_line: 1, end_line: 1, level: 1,
     spans: [{text: "Title", style: "plain"}]},
    {kind: "paragraph", start_line: 3, end_line: 3, level: 0,
     spans: [{text: "Some ", style: "plain"}, {text: "bold", style: "bold"},
             {text: " and a ", style: "plain"},
             {text: "link", style: "link", href: "https://example.com"}]},
    {kind: "list_item", start_line: 5, end_line: 5, level: 0, ordered: false, marker: "-",
     spans: [{text: "first bullet", style: "plain"}]},
    {kind: "list_item", start_line: 6, end_line: 6, level: 0, ordered: false, marker: "-",
     spans: [{text: "second bullet", style: "plain"}]},
    {kind: "code", start_line: 8, end_line: 10, level: 0, spans: [],
     code_text: "def f():\\n    pass", code_lang: "python"},
  ],
})"""


def open_preview(page):
    page.evaluate(PREVIEW_PAYLOAD)
    page.wait_for_selector(".md-block")


def test_preview_renders_blocks_not_raw_markdown(page):
    open_preview(page)
    assert page.locator(".md-block").count() == 5
    assert page.locator(".md-heading").count() == 1
    assert page.locator(".md-code-block").count() == 1

    # The point of a *preview*: none of the source syntax survives.
    texts = page.eval_on_selector_all(".md-block", "els => els.map(e => e.innerText)")
    joined = "\n".join(texts)
    assert "#" not in joined
    assert "**" not in joined
    assert "```" not in joined
    assert "](" not in joined
    assert "Title" in joined and "bold" in joined and "link" in joined


def test_link_is_not_an_anchor_and_keeps_its_url_as_a_tooltip(page):
    open_preview(page)
    link = page.locator(".md-link").first
    assert link.get_attribute("title") == "https://example.com"
    # Never an <a href> — a file-derived href would be an injection surface.
    assert page.locator(".md-block a").count() == 0


def test_preview_hides_the_diff_view_mode_toggle_and_shows_its_own_bar(page):
    open_preview(page)
    assert page.locator("#md-preview-bar").is_visible()
    assert not page.locator("#view-mode-toggle").is_visible()
    assert page.locator("#back-btn").is_visible()
    assert page.evaluate("() => __app.state.codeViewMode") == "md-preview"


def test_block_selection_expands_between_two_picks_and_restarts_on_a_third(page):
    open_preview(page)
    page.locator(".md-block").nth(0).dblclick()
    page.wait_for_timeout(150)
    assert page.locator(".md-block.md-in-range").count() == 1

    page.locator(".md-block").nth(3).dblclick()
    page.wait_for_timeout(150)
    assert page.locator(".md-block.md-in-range").count() == 4  # inclusive range
    assert not page.locator("#md-read-selection-btn").is_disabled()

    page.locator(".md-block").nth(4).dblclick()  # a 3rd starts over
    page.wait_for_timeout(150)
    assert page.locator(".md-block.md-in-range").count() == 1


def test_block_selection_does_not_contaminate_marked_lines(page):
    """The guard that matters most: markedLines is expanded into hidden LLM
    prompt context on every reply/request_change (see app/web/context.py's
    augment_with_marked_context). A preview selection must never end up
    there."""
    open_preview(page)
    page.locator(".md-block").nth(0).dblclick()
    page.wait_for_timeout(100)
    page.locator(".md-block").nth(3).dblclick()
    page.wait_for_timeout(150)

    assert page.evaluate("() => __app.state.mdSelectedBlocks.length") == 2
    assert page.evaluate("() => __app.state.markedLines.length") == 0
    assert "hidden" in (page.locator("#marked-lines-chip").get_attribute("class") or "")


def test_clear_button_empties_the_selection(page):
    open_preview(page)
    page.locator(".md-block").nth(1).dblclick()
    page.wait_for_timeout(150)
    page.click("#md-clear-selection-btn")
    page.wait_for_timeout(150)
    assert page.locator(".md-block.md-in-range").count() == 0
    assert page.locator("#md-read-selection-btn").is_disabled()


def test_select_gutter_is_a_real_button_with_a_dynamic_aria_label(page):
    open_preview(page)
    gutter = page.locator(".md-select-gutter").first
    assert gutter.get_attribute("aria-pressed") == "false"
    gutter.click()
    page.wait_for_timeout(150)
    gutter = page.locator(".md-select-gutter").first  # re-query: selection re-renders
    assert gutter.get_attribute("aria-pressed") == "true"
    assert gutter.get_attribute("aria-label") == "Deselect this block"


def test_back_leaves_the_preview_and_restores_the_diff(page):
    open_preview(page)
    page.click("#back-btn")
    page.wait_for_timeout(300)
    assert page.locator(".md-block").count() == 0
    assert page.locator(".code-line").count() > 0
    assert page.locator("#view-mode-toggle").is_visible()
    assert page.evaluate("() => __app.state.codeViewMode") == "diff"


def test_rerender_current_view_does_not_paint_a_diff_over_the_preview(page):
    """rerenderCurrentView has ~14 callers (mark toggles, every comment
    lifecycle event, ...). None of them may redraw a hunk on top of the
    preview."""
    open_preview(page)
    page.evaluate("() => __app.rerenderCurrentView()")
    page.wait_for_timeout(200)
    assert page.locator(".md-block").count() == 5
    assert page.locator(".code-line").count() == 0
    assert page.evaluate("() => __app.state.codeViewMode") == "md-preview"


def test_switching_diff_view_mode_does_not_disturb_the_preview(page):
    open_preview(page)
    page.evaluate("() => __app.setDiffViewMode('split')")
    page.wait_for_timeout(200)
    assert page.locator(".md-block").count() == 5
    assert page.evaluate("() => __app.state.codeViewMode") == "md-preview"
    # ...and the preference itself still round-trips for the diff view.
    assert page.evaluate("() => localStorage.getItem('ai_pear_review_diff_view_mode')") == "split"


def test_block_at_fraction_maps_audio_position_onto_weights(page):
    """The read-along highlight's arithmetic, isolated. Pure and top-level
    precisely so it's testable without a TTS service."""
    blocks = "[{block_index:0,weight:10},{block_index:1,weight:90}]"
    assert page.evaluate(f"() => __app.blockAtFraction({blocks}, 0)") == 0
    assert page.evaluate(f"() => __app.blockAtFraction({blocks}, 0.05)") == 0
    assert page.evaluate(f"() => __app.blockAtFraction({blocks}, 0.5)") == 1
    assert page.evaluate(f"() => __app.blockAtFraction({blocks}, 1)") == 1
    # Degenerate inputs must not throw or divide by zero.
    assert page.evaluate("() => __app.blockAtFraction([], 0.5)") is None
    assert page.evaluate("() => __app.blockAtFraction([{block_index:7,weight:0}], 0.5)") == 7


# --- Read-aloud pause/resume ----------------------------------------------
# Real audio bytes rather than a stub string, so the <audio> element takes
# the src the way it does in production. Playback itself is NOT asserted on:
# whether headless Chromium's autoplay policy actually lets an unmuted clip
# start is not this feature's contract, and the code already tolerates a
# rejected play() with a .catch(). What IS asserted is what the reviewer
# sees and what the module reports.


def _silent_wav_b64(seconds: float = 2.0) -> str:
    """A small but genuinely decodable 8-bit mono PCM WAV of silence."""
    rate = 8000
    frames = int(rate * seconds)
    data = bytes([0x80]) * frames  # 8-bit PCM silence sits at 0x80, not 0x00
    header = (
        b"RIFF"
        + struct.pack("<I", 36 + frames)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate, 1, 8)
        + b"data"
        + struct.pack("<I", frames)
    )
    return base64.b64encode(header + data).decode("ascii")


def enqueue_chunk(page, index: int, count: int = 2, blocks=(0, 1)):
    """Feeds one file_audio_chunk in, matching the preview's file_path and
    content_hash so the read-along highlight path runs for real too."""
    payload = {
        "file_path": "notes.md",
        "content_hash": "deadbeefdeadbeef",
        "chunk_index": index,
        "chunk_count": count,
        "mime_type": "audio/wav",
        "audio_base64": _silent_wav_b64(),
        "start_line": 1,
        "end_line": 6,
        "blocks": [{"block_index": b, "weight": 1.0, "partial": False} for b in blocks],
    }
    page.evaluate("p => __app.enqueueFileAudioChunk(p)", payload)
    page.wait_for_timeout(100)


def pause_label(page) -> str:
    return (page.locator("#md-pause-btn").inner_text() or "").strip()


def test_pause_button_is_hidden_until_a_read_is_in_flight(page):
    open_preview(page)
    assert not page.locator("#md-pause-btn").is_visible()
    assert not page.locator("#md-stop-btn").is_visible()

    enqueue_chunk(page, 0)
    assert page.locator("#md-pause-btn").is_visible()
    assert pause_label(page) == "Pause"
    assert page.evaluate("() => __app.fileAudioPaused") is False


def test_pause_holds_the_read_and_offers_resume(page):
    open_preview(page)
    enqueue_chunk(page, 0)

    page.click("#md-pause-btn")
    page.wait_for_timeout(100)
    assert page.evaluate("() => __app.fileAudioPaused") is True
    assert page.evaluate("() => document.getElementById('tts-audio').paused") is True
    assert pause_label(page) == "Resume"
    assert page.locator("#md-pause-btn").get_attribute("title") == "Resume reading"
    # Stop stays available alongside it — pausing is not a way out of the read.
    assert page.locator("#md-stop-btn").is_visible()


def test_resume_puts_the_button_back_to_pause(page):
    open_preview(page)
    enqueue_chunk(page, 0)
    page.click("#md-pause-btn")
    page.wait_for_timeout(100)

    page.click("#md-pause-btn")
    page.wait_for_timeout(100)
    assert page.evaluate("() => __app.fileAudioPaused") is False
    assert pause_label(page) == "Pause"
    assert page.locator("#md-pause-btn").get_attribute("title") == "Pause reading"


def test_a_chunk_arriving_during_a_pause_does_not_restart_playback(page):
    """The server keeps synthesizing through a pause, so chunks go on
    arriving into a queue that isn't draining. They must queue behind the
    held one. This is what pins fileAudioPlaying staying *true* while
    paused: flip it to false and enqueueFileAudioChunk's `if
    (!fileAudioPlaying) playNextFileAudioChunk()` guard would call play()
    on the newcomer and silently un-pause the reviewer."""
    open_preview(page)
    enqueue_chunk(page, 0, blocks=(0, 1))
    held = page.eval_on_selector_all(".md-block.md-in-chunk", "els => els.map(e => e.dataset.blockIndex)")
    assert held == ["0", "1"]

    page.click("#md-pause-btn")
    page.wait_for_timeout(100)
    enqueue_chunk(page, 1, blocks=(2, 3))

    assert page.evaluate("() => __app.fileAudioPaused") is True
    assert page.evaluate("() => document.getElementById('tts-audio').paused") is True
    still_held = page.eval_on_selector_all(".md-block.md-in-chunk", "els => els.map(e => e.dataset.blockIndex)")
    assert still_held == ["0", "1"], "the queued chunk took over while paused"


def test_stop_while_paused_does_not_leave_the_next_read_paused(page):
    """The regression that matters most: fileAudioPaused is cleared in one
    place (stopFileAudioQueue). Missing it would leave the NEXT read holding
    silently, with a button reading Resume over a queue that never started."""
    open_preview(page)
    enqueue_chunk(page, 0)
    page.click("#md-pause-btn")
    page.wait_for_timeout(100)
    assert page.evaluate("() => __app.fileAudioPaused") is True

    page.click("#md-stop-btn")
    page.wait_for_timeout(200)
    assert page.evaluate("() => __app.fileAudioPaused") is False
    assert not page.locator("#md-pause-btn").is_visible()
    assert not page.locator("#md-stop-btn").is_visible()

    enqueue_chunk(page, 0)
    assert page.evaluate("() => __app.fileAudioPaused") is False
    assert pause_label(page) == "Pause"
