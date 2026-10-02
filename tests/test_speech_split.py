"""Unit tests for app/speech_text.py's sizing half — the character/word
budget that decides how much text goes into one TTS request.

The property tests near the bottom (termination, no-loss) are the ones that
matter most: the splitter is a loop with a computed cut offset, so the
failure mode of getting it wrong is a hang or silently dropped speech, not
a visibly wrong answer.
"""

import pytest

from app.utils.speech_text import fits, sentence_segments, split_for_speech, word_count

LOREM = " ".join(f"word{i}" for i in range(200))
SENTENCES = " ".join(f"This is sentence number {i}." for i in range(40))


# --- fits() ---------------------------------------------------------------


def test_fits_with_no_limits_is_always_true():
    assert fits("anything at all", None, None)
    assert fits("x" * 10_000)


def test_fits_treats_zero_and_negative_as_unlimited():
    # An omitted key gives None, `max_words: 0` plainly means "off", and a
    # negative would loop forever downstream — all three mean the same.
    assert fits("x" * 500, 0, 0)
    assert fits("x" * 500, -1, -1)


def test_fits_char_boundary_is_inclusive():
    assert fits("x" * 10, 10, None)
    assert not fits("x" * 11, 10, None)


def test_fits_word_boundary_is_inclusive():
    assert fits("a b c", None, 3)
    assert not fits("a b c d", None, 3)


def test_fits_requires_both_limits_when_both_are_set():
    # 3 words but 100 chars: passes words, fails chars.
    assert not fits("x" * 40 + " y z", 20, 5)
    # short but too many words
    assert not fits("a b c d e f", 100, 3)
    assert fits("a b c", 100, 5)


def test_word_count_collapses_whitespace_runs():
    assert word_count("  a   b \n c  ") == 3
    assert word_count("") == 0


# --- the no-limit fast path ----------------------------------------------


def test_no_limits_returns_the_text_unchanged_as_one_piece():
    # Not just an optimization: the splitting path strips and re-joins on
    # sentence boundaries, so without the fast path, configuring no limit
    # would still quietly rewrite what gets spoken.
    text = "First.   Second.\n\nThird."
    assert split_for_speech(text) == [text]


def test_text_within_budget_is_returned_unchanged():
    text = "Short enough."
    assert split_for_speech(text, 100, 100) == [text]


def test_blank_input_returns_nothing_to_say():
    assert split_for_speech("") == []
    assert split_for_speech("   \n\t ") == []


# --- character budget -----------------------------------------------------


def test_char_limit_respected():
    pieces = split_for_speech(SENTENCES, 80, None)
    assert len(pieces) > 1
    assert all(len(p) <= 80 for p in pieces)


def test_char_limit_prefers_sentence_boundaries():
    pieces = split_for_speech(SENTENCES, 200, None)
    # every piece should end at a real sentence end, not mid-clause
    assert all(p.rstrip().endswith(".") for p in pieces)


def test_single_giant_word_terminates_and_loses_nothing():
    pieces = split_for_speech("x" * 500, 50, None)
    assert all(len(p) <= 50 for p in pieces)
    assert "".join(pieces) == "x" * 500


# --- word budget ----------------------------------------------------------


def test_word_limit_respected():
    pieces = split_for_speech(LOREM, None, 20)
    assert len(pieces) > 1
    assert all(word_count(p) <= 20 for p in pieces)


def test_word_limit_alone_never_splits_a_single_word():
    # A word cap cannot split a word, and must not invent a cut nobody
    # asked for — one 500-char word is one word, so it fits a cap of 1.
    giant = "x" * 500
    assert split_for_speech(giant, None, 1) == [giant]


def test_word_limit_with_generous_char_limit_is_what_binds():
    pieces = split_for_speech(LOREM, 100_000, 10)
    assert all(word_count(p) <= 10 for p in pieces)
    assert len(pieces) == pytest.approx(20, abs=2)


# --- both budgets ---------------------------------------------------------


def test_char_limit_binds_first_on_long_words():
    long_words = " ".join("supercalifragilistic" for _ in range(50))
    pieces = split_for_speech(long_words, 60, 100)
    assert all(len(p) <= 60 for p in pieces)


def test_word_limit_binds_first_on_short_words():
    short_words = " ".join("a" for _ in range(200))
    pieces = split_for_speech(short_words, 10_000, 15)
    assert all(word_count(p) <= 15 for p in pieces)


def test_both_limits_satisfied_simultaneously():
    pieces = split_for_speech(SENTENCES, 120, 12)
    assert all(fits(p, 120, 12) for p in pieces)


# --- properties: the ones that catch a hang or silent loss ---------------

ADVERSARIAL = [
    ("x" * 500, 50, None),
    ("x" * 500, None, 1),
    ("x" * 500, 50, 1),
    (" ".join("a" for _ in range(200)), 1, 1),
    (SENTENCES, 1, None),
    (SENTENCES, None, 1),
    (SENTENCES, 3, 2),
    (LOREM, 7, 3),
    ("word " * 100, 5, 5),
    ("a\n\nb\n\nc", 1, 1),
    ("one.  two.   three.", 4, 1),
]


@pytest.mark.parametrize("text,chars,words", ADVERSARIAL)
def test_always_terminates_and_every_piece_is_non_empty(text, chars, words):
    pieces = split_for_speech(text, chars, words)
    assert pieces, "a non-blank input must produce at least one piece"
    assert all(p.strip() for p in pieces), "an empty piece means the cut made no progress"


@pytest.mark.parametrize("text,chars,words", ADVERSARIAL)
def test_every_piece_fits_its_budget_unless_it_is_one_unsplittable_word(text, chars, words):
    for piece in split_for_speech(text, chars, words):
        # The one legitimate exception: a single word longer than the char
        # budget under a word-only cap can't be cut without inventing a
        # mid-word boundary, so it's emitted whole.
        if word_count(piece) <= 1 and chars is None:
            continue
        assert fits(piece, chars, words), f"{piece!r} exceeds {chars=} {words=}"


def _dense(text: str) -> str:
    """Every non-whitespace character, in order. Whitespace is excluded
    because the splitter strips around its cuts by design."""
    return "".join(text.split())


@pytest.mark.parametrize("text,chars,words", ADVERSARIAL)
def test_no_characters_are_lost_duplicated_or_reordered(text, chars, words):
    """Deliberately character-level, not word-level: a word longer than the
    whole character budget *must* be cut mid-word — that's the only way to
    make progress on it — so asserting words stay intact would forbid
    correct behavior. What must never happen is content going missing or
    coming back in the wrong order."""
    pieces = split_for_speech(text, chars, words)
    assert _dense("".join(pieces)) == _dense(text)


def test_words_are_kept_whole_when_the_budget_allows_it():
    # The complement of the test above: mid-word cuts are a last resort, so
    # with a budget comfortably larger than the longest word, none happen.
    pieces = split_for_speech(LOREM, 60, None)
    assert " ".join(pieces).split() == LOREM.split()


def test_internal_whitespace_inside_a_piece_is_not_normalized():
    """Guards the "slice by offset, never rebuild from split()" rule in
    _hard_split. A rejoin would collapse the double space below, silently
    changing the text actually spoken — and, in the markdown path, breaking
    the guarantee that a block's spoken text matches its rendered spans."""
    text = "alpha  beta gamma delta epsilon zeta eta theta"
    pieces = split_for_speech(text, 20, None)
    assert any("alpha  beta" in p for p in pieces)


# --- sentence_segments() (the chat read-along highlight) -------------------


def test_sentence_segments_one_per_sentence_in_order():
    segments = sentence_segments("First one here. Second one? Third!")
    assert [s["text"] for s in segments] == ["First one here.", "Second one?", "Third!"]


def test_sentence_segments_weigh_the_humanized_text():
    # The weight tracks what was synthesized ("config dot yaml"), but the
    # text stays as written, because the frontend looks for it on the page.
    [segment] = sentence_segments("Edit config.yaml.")
    assert segment["text"] == "Edit config.yaml."
    assert segment["weight"] == len("Edit config dot yaml.")


def test_sentence_segments_blank_is_empty():
    assert sentence_segments("   ") == []
