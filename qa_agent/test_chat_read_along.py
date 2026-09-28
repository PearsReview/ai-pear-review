"""The chat read-along highlight: the sentence of a presenter reply being
read aloud is highlighted as the audio plays (see static/js/audio.js).

Driven with synthetic payloads via the __app seam, like test_md_preview.py:
a real presenter turn needs a live model and its audio a live TTS service,
neither of which this environment runs. Playback position is faked by
shadowing the <audio> element's duration/currentTime and firing its
timeupdate handler, so no audio actually has to play.
"""

from __future__ import annotations

import json

import pytest

from app.utils.markdown_speech import block_to_payload, sanitize_persona_reply
from app.utils.speech_text import sentence_segments, split_for_speech

# A heading (spoken with a "." the page doesn't show), a paragraph whose
# second sentence ends inside `code`, and a code block spoken as a
# placeholder that isn't on the page at all.
TURN = """() => __app.appendTurn("presenter", "", {
  index: 0, total: 1, file_path: "sample.py",
  blocks: [
    {kind: "heading", level: 2, spans: [{text: "Retry loop", style: "plain"}]},
    {kind: "paragraph", level: 0, spans: [
      {text: "This adds a retry. It calls ", style: "plain"},
      {text: "fetch()", style: "code"},
      {text: ".", style: "plain"}]},
    {kind: "code", level: 0, spans: [], code_text: "x = 1", code_lang: "python"},
  ],
})"""

CHUNK = """() => __app.playAudio({
  mime_type: "audio/wav", audio_base64: "AA==", chunk_index: 0, chunk_count: 1,
  sentences: [
    {text: "Retry loop.", weight: 10},
    {text: "This adds a retry.", weight: 20},
    {text: "It calls fetch().", weight: 20},
    {text: "(code block omitted).", weight: 50},
  ],
})"""

HIGHLIGHTED = """() => {
  const h = CSS.highlights.get("tts-reading");
  return h ? [...h][0].toString() : null;
}"""


def _seek(page, fraction: float) -> None:
    page.evaluate(
        """(fraction) => {
            const audio = document.getElementById("tts-audio");
            Object.defineProperty(audio, "duration", {value: 10, configurable: true});
            Object.defineProperty(audio, "currentTime", {value: 10 * fraction, configurable: true, writable: true});
            audio.ontimeupdate();
        }""",
        fraction,
    )


@pytest.fixture
def speaking(page):
    page.evaluate("__app.state.ttsPrefOn = true")
    page.evaluate(TURN)
    page.evaluate(CHUNK)
    return page


def test_first_sentence_is_highlighted_as_the_clip_starts(speaking):
    # The heading was spoken as "Retry loop." — found without its extra ".".
    assert speaking.evaluate(HIGHLIGHTED) == "Retry loop"


def test_highlight_follows_playback_by_spoken_length(speaking):
    _seek(speaking, 0.2)  # weights 10/20/20/50: 20% is inside sentence two
    assert speaking.evaluate(HIGHLIGHTED) == "This adds a retry."
    _seek(speaking, 0.45)
    # Ends inside the code span — a range can cross elements a wrapper couldn't.
    assert speaking.evaluate(HIGHLIGHTED) == "It calls fetch()."


def test_a_sentence_not_on_the_page_clears_the_highlight(speaking):
    _seek(speaking, 0.9)  # the code-block placeholder
    assert speaking.evaluate(HIGHLIGHTED) is None


def test_stopping_the_audio_clears_the_highlight(speaking):
    speaking.evaluate("__app.send('stop')")
    assert speaking.evaluate(HIGHLIGHTED) is None


# --- The speaker button: hear a reply on request, with voice output off ---

SPOKEN_TURN = """() => __app.appendTurn("presenter", "", {
  index: 0, total: 1, file_path: "sample.py", spoken: "It retries three times.",
  blocks: [{kind: "paragraph", level: 0, spans: [{text: "It retries three times.", style: "plain"}]}],
})"""


@pytest.fixture
def voice_off(page):
    page.evaluate("__app.state.ttsPrefOn = false")
    page.evaluate("window.__sent = []; __app.sendHook = (type, payload) => __sent.push({type, payload});")
    page.evaluate(SPOKEN_TURN)
    return page


def test_a_reply_with_spoken_text_gets_a_speaker_button(voice_off):
    assert voice_off.locator(".turn.presenter .speak-turn-btn").count() == 1


def test_a_turn_without_spoken_text_falls_back_to_its_own_text(page):
    page.evaluate("window.__sent = []; __app.sendHook = (type, payload) => __sent.push({type, payload});")
    page.evaluate(
        """() => __app.appendTurn("presenter", "It retries.", {index: 0, total: 1, file_path: "sample.py"})"""
    )
    page.click(".speak-turn-btn")
    assert page.evaluate("__sent.filter(m => m.type === 'speak_turn')[0].payload.text") == "It retries."


def test_a_turn_with_nothing_to_say_gets_no_button(page):
    page.evaluate(TURN)  # no "spoken" field and empty text
    assert page.locator(".speak-turn-btn").count() == 0


def test_clicking_it_asks_for_that_reply_even_with_voice_off(voice_off):
    voice_off.click(".speak-turn-btn")
    sent = voice_off.evaluate("__sent.filter(m => m.type === 'speak_turn')")
    assert sent == [{"type": "speak_turn", "payload": {"text": "It retries three times."}}]


CLIP = """() => __app.playTurnAudio({mime_type: "audio/wav", audio_base64: "AA==", chunk_index: 0,
    chunk_count: 1, sentences: [{text: "It retries three times.", weight: 10}]})"""

BUTTON_CLASSES = "() => [...document.querySelector('.speak-turn-btn').classList].sort()"


def _clip_ends(page) -> None:
    page.evaluate("document.getElementById('tts-audio').onended()")


def test_its_audio_plays_and_highlights_despite_voice_off(voice_off):
    voice_off.click(".speak-turn-btn")
    voice_off.evaluate(CLIP)
    assert voice_off.evaluate(HIGHLIGHTED) == "It retries three times."


def test_a_second_click_after_it_finishes_replays_without_asking_again(voice_off):
    voice_off.click(".speak-turn-btn")
    voice_off.evaluate(CLIP)
    _clip_ends(voice_off)
    voice_off.click(".speak-turn-btn")
    assert voice_off.evaluate("__sent.filter(m => m.type === 'speak_turn').length") == 1
    assert voice_off.evaluate(HIGHLIGHTED) == "It retries three times."


# --- Button state: lit while loading/playing, spinner while generating ----


def test_button_spins_while_speech_is_generated(voice_off):
    voice_off.click(".speak-turn-btn")
    assert voice_off.evaluate(BUTTON_CLASSES) == ["is-active", "is-loading", "replay-btn", "speak-turn-btn"]


def test_button_stays_lit_but_stops_spinning_once_audio_plays(voice_off):
    voice_off.click(".speak-turn-btn")
    voice_off.evaluate(CLIP)
    assert voice_off.evaluate(BUTTON_CLASSES) == ["is-active", "replay-btn", "speak-turn-btn"]


def test_button_goes_dark_when_the_audio_finishes(voice_off):
    voice_off.click(".speak-turn-btn")
    voice_off.evaluate(CLIP)
    _clip_ends(voice_off)
    assert voice_off.evaluate(BUTTON_CLASSES) == ["replay-btn", "speak-turn-btn"]


def test_button_spins_again_if_playback_catches_up_with_synthesis(voice_off):
    voice_off.click(".speak-turn-btn")
    voice_off.evaluate(CLIP.replace("chunk_count: 1", "chunk_count: 2"))
    _clip_ends(voice_off)  # clip 2 of 2 not here yet
    assert "is-loading" in voice_off.evaluate(BUTTON_CLASSES)


def test_clicking_a_spinning_button_cancels_it(voice_off):
    voice_off.click(".speak-turn-btn")
    voice_off.click(".speak-turn-btn")
    assert voice_off.evaluate("__sent.filter(m => m.type === 'stop').length") == 1
    assert voice_off.evaluate(BUTTON_CLASSES) == ["replay-btn", "speak-turn-btn"]


def test_button_goes_dark_if_the_tts_service_fails(voice_off):
    voice_off.click(".speak-turn-btn")
    voice_off.evaluate("__app.updateStatus({tts: false})")
    assert voice_off.evaluate(BUTTON_CLASSES) == ["replay-btn", "speak-turn-btn"]


def test_a_look_deeper_answer_gets_a_speaker_button(page):
    page.evaluate(
        """() => __app.appendTurn("deeper", "It is called from two places.", {index: 0, total: 1,
            file_path: "sample.py", agent: "Cline", model: "m", spoken: "It is called from two places."})"""
    )
    assert page.locator(".turn.deeper .speak-turn-btn").count() == 1


# --- Matching real server output: what a Look deeper answer looks like ----

# Everything that made sentences go unhighlighted before, in one answer:
# emoji the voice drops, a table (spoken "cell, cell, cell."), inline code,
# a link, a quote and a fenced block.
RICH_ANSWER = """It is read in two places, and both depend on the new retry count.

1. `checkout()` in **app/billing.py** calls `fetch_invoice()` inside a loop.
2. The nightly job (`jobs/reconcile.py`) calls it once per account ✅ and ignores failures.

| Caller | File | Retries |
|---|---|---|
| `checkout()` | app/billing.py | 3 |
| `reconcile()` | jobs/reconcile.py | 0 |

> Note: the job was written before retries existed.

```python
for attempt in range(3):
    fetch_invoice()
```

See [the docs](https://example.com/retry) for why three was chosen. The nightly job could now take 3x longer ⚠️ on a bad night.
"""


def test_every_spoken_sentence_of_a_rich_answer_is_found(page):
    """Built with the server's own pipeline — sanitize, split, segment — so
    this fails if either side drifts, not just the frontend."""
    blocks, spoken = sanitize_persona_reply(RICH_ANSWER, "")
    sentences = [s["text"] for piece in split_for_speech(spoken, 200) for s in sentence_segments(piece)]
    payload = {"index": 0, "total": 1, "file_path": "x.py", "agent": "Cline", "model": "m", "spoken": spoken}
    payload["blocks"] = [block_to_payload(b) for b in blocks]
    page.evaluate(f"() => __app.appendTurn('deeper', '', {json.dumps(payload)})")
    found = page.evaluate(
        f"() => __app.locateSentences(document.querySelector('.turn.deeper .turn-body'), {json.dumps(sentences)})"
    )

    missing = [s for s, f in zip(sentences, found, strict=True) if f is None]
    assert missing == ["(code block omitted)."]  # code is never on the page as speech
    by_sentence = dict(zip(sentences, found, strict=True))
    assert by_sentence["checkout(), app/billing.py, 3."] is not None  # a table row
    assert "ignores failures." in by_sentence[next(s for s in sentences if "nightly job (" in s)]  # emoji dropped
