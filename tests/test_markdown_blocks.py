"""Unit tests for app/markdown_speech.py's block parser — the line-aware
half. Chunking, weights and composition live in test_markdown_speech.py.

The parser is what everything else in the markdown-preview feature stands
on: the preview renders blocks, selection picks blocks, and the read-along
highlight resolves audio position to a block. So these tests lean hard on
line provenance and on the spans/spoken-text invariant, which are the two
properties the rest of the feature silently assumes.
"""

from pathlib import Path

import pytest

from app.utils.markdown_speech import (
    Span,
    _ensure_pause,
    _spoken_from_spans,
    parse_markdown_blocks,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def kinds(blocks):
    return [b.kind for b in blocks]


def only(blocks, kind):
    return [b for b in blocks if b.kind == kind]


# --- block classification -------------------------------------------------


def test_heading_level_and_text():
    blocks = parse_markdown_blocks("# One\n\n### Three\n")
    assert kinds(blocks) == ["heading", "heading"]
    assert blocks[0].level == 1
    assert blocks[0].spoken_text == "One."
    assert blocks[1].level == 3


def test_paragraph_folds_wrapped_lines_into_one_block():
    blocks = parse_markdown_blocks("first line\nsecond line\n\nnext para\n")
    assert kinds(blocks) == ["paragraph", "paragraph"]
    assert blocks[0].spoken_text == "first line second line"
    assert (blocks[0].start_line, blocks[0].end_line) == (1, 2)
    assert (blocks[1].start_line, blocks[1].end_line) == (4, 4)


def test_list_items_are_separate_blocks_each_with_a_pause():
    blocks = parse_markdown_blocks("- Fast\n- Reliable\n- Secure\n")
    assert kinds(blocks) == ["list_item"] * 3
    assert [b.spoken_text for b in blocks] == ["Fast.", "Reliable.", "Secure."]
    assert [b.start_line for b in blocks] == [1, 2, 3]


def test_wrapped_list_item_folds_into_one_block():
    blocks = parse_markdown_blocks("- a bullet that\n  wraps onto a second line\n- next\n")
    assert kinds(blocks) == ["list_item", "list_item"]
    assert blocks[0].spoken_text == "a bullet that wraps onto a second line."
    assert (blocks[0].start_line, blocks[0].end_line) == (1, 2)
    assert blocks[1].start_line == 3


def test_ordered_list_keeps_the_authors_marker():
    blocks = parse_markdown_blocks("1. one\n2. two\n7. seven\n")
    assert all(b.ordered for b in blocks)
    assert [b.marker for b in blocks] == ["1.", "2.", "7."]


def test_unordered_list_is_not_ordered():
    blocks = parse_markdown_blocks("- a\n* b\n+ c\n")
    assert not any(b.ordered for b in blocks)


def test_nested_list_level():
    blocks = parse_markdown_blocks("- top\n  - nested\n    - deeper\n")
    assert [b.level for b in blocks] == [0, 1, 2]


def test_wrapped_blockquote_is_one_block_with_one_pause():
    blocks = parse_markdown_blocks("> first part\n> second part\n")
    assert kinds(blocks) == ["quote"]
    assert blocks[0].spoken_text == "first part second part."
    assert (blocks[0].start_line, blocks[0].end_line) == (1, 2)


def test_separate_blockquotes_are_separate_blocks():
    blocks = parse_markdown_blocks("> first\n\n> second\n")
    assert kinds(blocks) == ["quote", "quote"]
    assert blocks[0].spoken_text == "first."
    assert blocks[1].spoken_text == "second."


def test_horizontal_rule_is_a_rule_and_is_never_spoken():
    blocks = parse_markdown_blocks("before\n\n---\n\nafter\n")
    assert kinds(blocks) == ["paragraph", "rule", "paragraph"]
    assert blocks[1].spoken_text == ""


def test_standalone_dashes_are_a_rule_not_a_table_separator():
    # The old substitution passes had _HR_RE and _TABLE_SEPARATOR_RE both
    # matching a bare "---", agreeing only by accident of ordering. A
    # separator is now only recognised directly after a table row.
    blocks = parse_markdown_blocks("---\n")
    assert kinds(blocks) == ["rule"]


# --- tables ---------------------------------------------------------------


def test_table_header_consumes_its_separator_row():
    blocks = parse_markdown_blocks("| A | B |\n| --- | --- |\n| 1 | 2 |\n")
    assert kinds(blocks) == ["table_row", "table_row"]
    assert blocks[0].is_header
    assert not blocks[1].is_header
    # the header block spans both its own line and the separator it ate
    assert (blocks[0].start_line, blocks[0].end_line) == (1, 2)
    assert (blocks[1].start_line, blocks[1].end_line) == (3, 3)


def test_table_cells_and_spoken_form():
    blocks = parse_markdown_blocks("| A | B |\n| --- | --- |\n| 1 | 2 |\n")
    assert blocks[0].cells == ["A", "B"]
    assert blocks[0].spoken_text == "A, B."
    assert blocks[1].spoken_text == "1, 2."


def test_consecutive_table_rows_share_a_group_and_a_gap_starts_a_new_one():
    blocks = parse_markdown_blocks("| A |\n| 1 |\n\n| B |\n| 2 |\n")
    groups = [b.table_group for b in only(blocks, "table_row")]
    assert groups[0] == groups[1]
    assert groups[2] == groups[3]
    assert groups[0] != groups[2]


def test_stray_separator_row_emits_nothing():
    blocks = parse_markdown_blocks("| --- | --- |\n")
    assert blocks == []


# --- code fences ----------------------------------------------------------


def test_code_fence_lines_lang_and_spoken_placeholder():
    blocks = parse_markdown_blocks("before\n\n```python\ndef f():\n    pass\n```\n\nafter\n")
    assert kinds(blocks) == ["paragraph", "code", "paragraph"]
    code = blocks[1]
    assert code.code_lang == "python"
    assert code.code_text == "def f():\n    pass"
    # start/end are the fence lines themselves, so selecting the block
    # selects the whole fenced region in the source
    assert (code.start_line, code.end_line) == (3, 6)
    assert code.spoken_text == "(code block omitted)."


def test_unclosed_code_fence_runs_to_eof_without_hanging():
    blocks = parse_markdown_blocks("```\nnever closed\nstill going\n")
    assert kinds(blocks) == ["code"]
    assert blocks[0].end_line == 3
    assert "never closed" in blocks[0].code_text


def test_markdown_inside_a_code_fence_is_not_parsed():
    blocks = parse_markdown_blocks("```\n# not a heading\n- not a list\n```\n")
    assert kinds(blocks) == ["code"]


# --- inline spans ---------------------------------------------------------


def spans_of(md):
    blocks = parse_markdown_blocks(md)
    return blocks[0].spans


def test_bold_and_italic_spans():
    spans = spans_of("plain **bold** and _italic_ end")
    assert [(s.text, s.style) for s in spans] == [
        ("plain ", "plain"),
        ("bold", "bold"),
        (" and ", "plain"),
        ("italic", "italic"),
        (" end", "plain"),
    ]


def test_strikethrough_span():
    assert [(s.text, s.style) for s in spans_of("~~gone~~")] == [("gone", "strike")]


def test_inline_code_span_keeps_the_identifier_intact():
    spans = spans_of("call `get_user_profile` now")
    assert Span("get_user_profile", "code") in spans


def test_dunder_stays_literal_and_plain():
    # __init__ is flanked by real word boundaries, so boundary guards alone
    # can't tell it from genuine __bold__ — it must stay literal in both
    # the preview and the audio.
    spans = spans_of("override __init__ please")
    assert len(spans) == 1
    assert spans[0].style == "plain"
    assert "__init__" in spans[0].text


def test_snake_case_is_not_italicised():
    spans = spans_of("call get_user_profile now")
    assert len(spans) == 1
    assert spans[0].style == "plain"
    assert spans[0].text == "call get_user_profile now"


def test_link_splits_text_from_href_and_drops_the_url_from_speech():
    spans = spans_of("see [the docs](https://example.com/x) now")
    link = [s for s in spans if s.style == "link"]
    assert len(link) == 1
    assert link[0].text == "the docs"
    assert link[0].href == "https://example.com/x"
    assert "example.com" not in parse_markdown_blocks("see [the docs](https://example.com/x)")[0].spoken_text


def test_image_becomes_its_alt_text():
    spans = spans_of("![a diagram](img.png)")
    assert [(s.text, s.style) for s in spans] == [("a diagram", "plain")]


def test_adjacent_plain_spans_are_merged():
    # the dunder above emits a plain span mid-string; it must not fragment
    # the surrounding prose into three separate plain runs
    spans = spans_of("a __init__ b")
    assert len(spans) == 1


# --- the spans/spoken invariant -------------------------------------------

PAUSED_KINDS = {"heading", "list_item", "quote", "table_row"}


def test_spoken_text_is_derived_from_spans_for_every_block_of_the_fixture():
    """The invariant the whole highlight feature rests on: what the preview
    renders and what the voice says come from one source, so they cannot
    drift apart."""
    fixture = REPO_ROOT / "tests" / "fixtures" / "tts_test.md"
    if not fixture.exists():
        pytest.skip("tts_test.md fixture not present")
    blocks = parse_markdown_blocks(fixture.read_text(encoding="utf-8"))
    assert blocks
    for block in blocks:
        if block.kind in ("code", "rule"):
            continue
        expected = _spoken_from_spans(block.spans, pause=block.kind in PAUSED_KINDS)
        assert block.spoken_text == expected, f"{block.kind} at line {block.start_line}"


def test_ensure_pause_does_not_double_punctuate():
    assert _ensure_pause("done!") == "done!"
    assert _ensure_pause("done") == "done."
    assert _ensure_pause("") == ""


def test_emoji_stay_in_spans_but_leave_the_spoken_text():
    blocks = parse_markdown_blocks("Ship it \U0001f680 today")
    rendered = "".join(s.text for s in blocks[0].spans)
    assert "\U0001f680" in rendered
    assert "\U0001f680" not in blocks[0].spoken_text


def test_international_text_survives_in_both_forms():
    blocks = parse_markdown_blocks("café déjà vu Привет 你好")
    assert blocks[0].spoken_text == "café déjà vu Привет 你好"


# --- line provenance ------------------------------------------------------


def test_blocks_are_ordered_non_overlapping_and_within_the_file():
    fixture = REPO_ROOT / "tests" / "fixtures" / "tts_test.md"
    if not fixture.exists():
        pytest.skip("tts_test.md fixture not present")
    source = fixture.read_text(encoding="utf-8")
    total = len(source.split("\n"))
    blocks = parse_markdown_blocks(source)
    previous_end = 0
    for block in blocks:
        assert 1 <= block.start_line <= block.end_line <= total
        assert block.start_line > previous_end, f"{block.kind} overlaps the block before it"
        previous_end = block.end_line


def test_a_blocks_line_range_actually_contains_its_own_text():
    source = "# Title\n\nA paragraph here.\n\n- bullet one\n"
    lines = source.split("\n")
    for block in parse_markdown_blocks(source):
        raw = "\n".join(lines[block.start_line - 1 : block.end_line])
        for span in block.spans:
            assert span.text.strip() in raw


def test_empty_and_whitespace_only_input():
    assert parse_markdown_blocks("") == []
    assert parse_markdown_blocks("\n\n   \n\t\n") == []
