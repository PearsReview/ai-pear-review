"""Unit tests for app/markdown_speech.py's chunking half — packing blocks
into TTS-sized chunks, and the per-block weights the frontend uses to
interpolate which block the audio is currently on.

Block parsing is covered separately in test_markdown_blocks.py.
"""

from app.utils.markdown_speech import (
    Block,
    chunk_blocks,
    parse_markdown_blocks,
    prepare_file_for_speech,
    strip_unreadable_chars,
)
from app.utils.speech_text import humanize_for_speech


def para(text, start=1, end=1):
    """A minimal paragraph block — the chunker only reads spoken_text and
    the line range, so the spans/kind detail parsing would add is noise here."""
    return Block(kind="paragraph", start_line=start, end_line=end, spoken_text=text)


# --- character cleanup (unchanged behavior, now applied per block) --------


def test_strip_unreadable_chars_removes_emoji_and_control_chars():
    text = "Looks good \U0001f600! Ship it ✅\x07"
    out = strip_unreadable_chars(text)
    assert "\U0001f600" not in out
    assert "✅" not in out
    assert "\x07" not in out
    assert "Looks good" in out and "Ship it" in out


def test_strip_unreadable_chars_preserves_international_text():
    text = "café — déjà vu — Привет — 你好"
    assert strip_unreadable_chars(text) == text


# --- packing --------------------------------------------------------------


def test_empty_input():
    assert chunk_blocks([], 100) == []
    assert prepare_file_for_speech("", 100) == []
    assert prepare_file_for_speech("   \n\n  \n", 100) == []


def test_chunks_respect_max_chars():
    blocks = [para(f"Paragraph number {i} with some words in it.", i, i) for i in range(20)]
    chunks = chunk_blocks(blocks, 100)
    assert chunks
    assert all(len(c.text) <= 100 for c in chunks)


def test_every_block_appears_exactly_once_across_chunks():
    blocks = [para(f"Paragraph number {i}.", i, i) for i in range(20)]
    chunks = chunk_blocks(blocks, 100)
    seen = [m.block_index for c in chunks for m in c.blocks]
    assert sorted(seen) == list(range(20))


def test_oversized_block_splits_on_sentence_boundaries():
    long_text = " ".join(f"This is sentence {i}." for i in range(30))
    chunks = chunk_blocks([para(long_text)], 80)
    assert len(chunks) > 1
    assert all(len(c.text) <= 80 for c in chunks)


def test_run_on_text_with_no_punctuation_hard_splits():
    chunks = chunk_blocks([para(("word " * 100).strip())], 50)
    assert len(chunks) > 1
    assert all(len(c.text) <= 50 for c in chunks)


def test_single_giant_word_terminates():
    chunks = chunk_blocks([para("x" * 500)], 50)
    assert all(len(c.text) <= 50 for c in chunks)
    assert "".join(c.text for c in chunks) == "x" * 500


# --- line provenance ------------------------------------------------------


def test_chunk_line_range_spans_its_blocks():
    blocks = [para("one.", 3, 4), para("two.", 6, 6), para("three.", 9, 11)]
    chunks = chunk_blocks(blocks, 1000)
    assert len(chunks) == 1
    assert (chunks[0].start_line, chunks[0].end_line) == (3, 11)


def test_rules_are_never_chunked():
    blocks = [para("before.", 1, 1), Block(kind="rule", start_line=2, end_line=2), para("after.", 3, 3)]
    chunks = chunk_blocks(blocks, 1000)
    indices = [m.block_index for c in chunks for m in c.blocks]
    assert indices == [0, 2]  # the rule at index 1 never appears


def test_oversized_block_appears_in_every_chunk_it_touches_marked_partial():
    long_text = " ".join(f"Sentence {i} of a very long paragraph." for i in range(40))
    chunks = chunk_blocks([para(long_text, 5, 7)], 200)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.blocks) == 1
        assert chunk.blocks[0].block_index == 0
        assert chunk.blocks[0].partial
        assert (chunk.blocks[0].start_line, chunk.blocks[0].end_line) == (5, 7)


# --- weights (what the read-along highlight interpolates over) ------------


def test_weights_are_measured_on_humanized_text():
    # humanize_for_speech expands config.yaml -> "config dot yaml" before
    # synthesis, so a weight taken from the raw text would describe audio
    # that was never produced.
    spoken = "edit config.yaml now"
    chunk = chunk_blocks([para(spoken)], 1000)[0]
    assert chunk.blocks[0].weight == len(humanize_for_speech(spoken))
    assert chunk.blocks[0].weight > len(spoken)


def test_humanization_identity_holds_for_every_chunk():
    """The identity that makes interpolation trustworthy: weights are
    computed per block, but the server humanizes the whole chunk in one
    call at synthesis time. Because blocks join with "\\n\\n" and neither
    identifier pattern in speech_text.py can match across a newline, the
    two agree exactly. If a future change to speech_text.py breaks this,
    the highlight silently desyncs and nothing else would catch it."""
    source = (
        "# Setup\n\n"
        "Edit config.yaml and run run.py.\n\n"
        "- call get_user_profile\n"
        "- check MAX_RETRY_COUNT\n\n"
        "Then visit 127.0.0.1 to confirm.\n"
    )
    chunks = prepare_file_for_speech(source, 1000)
    assert chunks
    for chunk in chunks:
        expected = len(humanize_for_speech(chunk.text)) - 2 * (len(chunk.blocks) - 1)
        assert sum(m.weight for m in chunk.blocks) == expected


def test_humanization_identity_holds_across_many_small_chunks():
    source = "\n\n".join(f"Paragraph {i} mentions config.yaml and get_user_profile." for i in range(12))
    chunks = prepare_file_for_speech(source, 120)
    assert len(chunks) > 1
    for chunk in chunks:
        expected = len(humanize_for_speech(chunk.text)) - 2 * (len(chunk.blocks) - 1)
        assert sum(m.weight for m in chunk.blocks) == expected


def test_weights_are_positive_so_interpolation_cannot_divide_by_zero():
    chunks = prepare_file_for_speech("# A\n\nSome text.\n\n- item\n", 1000)
    for chunk in chunks:
        assert sum(m.weight for m in chunk.blocks) > 0


# --- end to end -----------------------------------------------------------


def test_prepare_file_for_speech_end_to_end():
    md = "# Title\n\nSome **bold** text with a [link](https://x).\n\n```\ncode here\n```\n"
    chunks = prepare_file_for_speech(md, 1000)
    assert len(chunks) == 1
    text = chunks[0].text
    assert "Title" in text
    assert "bold" in text
    assert "link" in text
    assert "https://x" not in text
    assert "code here" not in text
    assert "(code block omitted)" in text


def test_include_filter_keeps_block_indices_absolute():
    """Reading a selection must not renumber blocks. block_index is what the
    open preview keys its highlight on, so it has to index the whole
    document — filtering by slicing the list first would restart at 0 and
    light up the wrong paragraphs."""
    blocks = [para(f"Paragraph {i}.", i + 1, i + 1) for i in range(6)]
    chunks = chunk_blocks(blocks, 1000, include={3, 4})
    indices = [m.block_index for c in chunks for m in c.blocks]
    assert indices == [3, 4]
    assert "Paragraph 3." in chunks[0].text
    assert "Paragraph 0." not in chunks[0].text


def test_include_filter_still_skips_unspoken_blocks():
    blocks = [para("a.", 1, 1), Block(kind="rule", start_line=2, end_line=2), para("b.", 3, 3)]
    chunks = chunk_blocks(blocks, 1000, include={0, 1, 2})
    indices = [m.block_index for c in chunks for m in c.blocks]
    assert indices == [0, 2]


def test_no_include_filter_chunks_everything():
    blocks = [para(f"P{i}.", i + 1, i + 1) for i in range(4)]
    chunks = chunk_blocks(blocks, 1000)
    assert [m.block_index for c in chunks for m in c.blocks] == [0, 1, 2, 3]


def test_word_limit_is_respected_and_loses_no_blocks():
    blocks = [para(" ".join(f"w{j}" for j in range(8)) + ".", i + 1, i + 1) for i in range(12)]
    chunks = chunk_blocks(blocks, None, max_words=20)
    assert len(chunks) > 1
    assert all(len(c.text.split()) <= 20 for c in chunks)
    assert sorted(m.block_index for c in chunks for m in c.blocks) == list(range(12))


def test_word_limit_binds_when_char_limit_is_generous():
    blocks = [para(" ".join("a" for _ in range(10)) + ".", i + 1, i + 1) for i in range(10)]
    by_chars_only = chunk_blocks(blocks, 100_000)
    by_words = chunk_blocks(blocks, 100_000, max_words=25)
    assert len(by_chars_only) == 1  # everything fits on chars alone
    assert len(by_words) > 1  # the word cap is what splits it


def test_humanization_identity_holds_under_a_word_limit():
    """The weights identity is what the read-along highlight rests on, and
    it must not care *why* a chunk boundary fell where it did. Same
    assertion as the char-limit version, driven by the word budget instead."""
    source = "\n\n".join(
        f"Paragraph {i} mentions config.yaml and get_user_profile and MAX_RETRY_COUNT." for i in range(12)
    )
    chunks = prepare_file_for_speech(source, 100_000, max_words=15)
    assert len(chunks) > 1
    for chunk in chunks:
        expected = len(humanize_for_speech(chunk.text)) - 2 * (len(chunk.blocks) - 1)
        assert sum(m.weight for m in chunk.blocks) == expected


def test_word_limit_marks_an_oversized_block_partial():
    long_text = " ".join(f"word{i}" for i in range(200)) + "."
    chunks = chunk_blocks([para(long_text, 5, 7)], None, max_words=20)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.blocks) == 1
        assert chunk.blocks[0].partial
        assert (chunk.blocks[0].start_line, chunk.blocks[0].end_line) == (5, 7)


def test_no_limits_at_all_puts_everything_in_one_chunk():
    blocks = [para(f"Paragraph {i}.", i + 1, i + 1) for i in range(30)]
    chunks = chunk_blocks(blocks, None)
    assert len(chunks) == 1


def test_block_indices_are_indices_into_the_parsed_block_list():
    source = "# Heading\n\nA paragraph.\n\n---\n\nAnother paragraph.\n"
    blocks = parse_markdown_blocks(source)
    chunks = chunk_blocks(blocks, 1000)
    for chunk in chunks:
        for member in chunk.blocks:
            assert 0 <= member.block_index < len(blocks)
            # the member's line range must match the block it points at
            assert blocks[member.block_index].start_line == member.start_line
            assert blocks[member.block_index].end_line == member.end_line
