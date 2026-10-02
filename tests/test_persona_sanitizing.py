"""markdown_speech.sanitize_persona_reply — dropping the two shapes of
noise recorded in production narration, and nothing else.

Two corpora back these tests, not hand-picked examples:

  - qa_agent/results/generated_narration_pack.json, if a generated-suite
    run has produced one — real output from a real model this session.
  - A handful of verbatim strings recorded earlier in this same session,
    kept here directly so the tests don't depend on that file existing.

The property that matters most is asymmetric: a missed announcement or
fence is a cosmetic annoyance a reader skims past, but an over-eager strip
deletes real content a reviewer needed. So "clean narration survives
untouched" gets exactly as much test weight as "known noise is caught".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.utils.markdown_speech import (
    _is_announcement,
    parse_markdown_blocks,
    sanitize_persona_reply,
)

_PACK_PATH = Path(__file__).resolve().parent.parent / "qa_agent" / "results" / "generated_narration_pack.json"

# Verbatim model output, recorded from real runs. Kept as a fixed fallback corpus independent of whether a
# generated-suite run has produced a fresh pack.
_RECORDED = [
    (
        "Sure, here's the diff hunk for shipment.py:\n\n"
        "```diff\n@@ -37,4 +39,4 @@ def audit_pallet(pallet):\n def reserve_sku(sku):\n"
        '    """Reserve one sku."""\n    total = len(str(sku))\n-    return total\n+    return max(total, 1)\n```\n\n'
        "I made this change to ensure the function always returns a non-zero value.",
        "@@ -37,4 +39,4 @@ def audit_pallet(pallet):\n def reserve_sku(sku):\n"
        '    """Reserve one sku."""\n    total = len(str(sku))\n-    return total\n+    return max(total, 1)',
    ),
    (
        "Sure, I'll present this hunk.\n\n"
        "The change in shipment.py modifies the reserve_sku function to use max(total, 1).",
        "",
    ),
    (
        "Sure, let's go through this hunk together.\n\n"
        "This change is adding a check to ensure the item parameter is not None.",
        "",
    ),
]


@pytest.fixture(scope="module")
def corpus() -> list[dict]:
    if not _PACK_PATH.exists():
        pytest.skip(f"no narration pack at {_PACK_PATH} — run pytest qa_agent/generated/ first")
    data = json.loads(_PACK_PATH.read_text(encoding="utf-8"))
    return data["entries"]


def test_a_diff_fence_dump_is_stripped_to_the_real_narration():
    text, diff = _RECORDED[0]
    blocks, spoken = sanitize_persona_reply(text, diff)
    assert "@@" not in spoken
    assert "```" not in spoken
    assert "always returns a non-zero value" in spoken
    assert not any(b.kind == "code" for b in blocks)


def test_a_leading_announcement_sentence_is_dropped():
    text, diff = _RECORDED[1]
    blocks, spoken = sanitize_persona_reply(text, diff)
    assert "Sure, I'll present" not in spoken
    assert "modifies the reserve_sku function" in spoken
    assert len(blocks) == 1


def test_lets_is_caught_alongside_ill_and_let_me():
    """Found by running the first version of this filter against the real
    corpus rather than by inspection: "Sure, let's go through this hunk
    together." wasn't covered by the original i'll/let me/i can pattern."""
    text, diff = _RECORDED[2]
    _blocks, spoken = sanitize_persona_reply(text, diff)
    assert "let's go through" not in spoken
    assert "check to ensure the item parameter" in spoken


@pytest.mark.parametrize(
    "text",
    [
        "This adds a validation check so the item is never None.",
        "I changed `return total` to `return max(total, 1)` to avoid a zero count.",
        "The `audit_item` function now raises a ValueError for missing input.",
        "Sure, that makes sense to me.",  # agreement, not an announcement — must survive
        "Of course the guard is redundant, since callers already check.",
    ],
)
def test_ordinary_narration_is_never_touched(text: str):
    blocks, spoken = sanitize_persona_reply(text, diff_context="")
    assert blocks == parse_markdown_blocks(text), "a clean reply must not lose or reorder any block"
    # Only the markdown decoration spoken_text always strips (backticks,
    # emphasis markers) may differ — never whole words or sentences.
    assert spoken.replace("`", "") == spoken


def test_a_code_block_that_is_not_the_diff_survives():
    """The failure mode this whole feature exists to avoid: deleting real
    content. A genuinely new snippet — not a re-paste of the hunk shown —
    must render, not disappear."""
    text = "Here's a smaller repro:\n\n```python\nx = compute(None)\n```\n\nThat's the case that breaks."
    blocks, _spoken = sanitize_persona_reply(text, diff_context="totally different diff content")
    assert any(b.kind == "code" and "compute(None)" in b.code_text for b in blocks)


def test_a_diff_labelled_fence_is_dropped_even_without_diff_context():
    """The language tag alone (```diff) is a strong enough signal on its
    own — this must work even for a reply where no diff was passed in,
    e.g. explore mode has no hunk at all."""
    text = "Sure, here's the change:\n\n```diff\n+x = 1\n```\n\nThat's it."
    blocks, _spoken = sanitize_persona_reply(text, diff_context="")
    assert not any(b.kind == "code" for b in blocks)


def test_a_bare_diff_header_is_dropped_even_in_an_unlabelled_fence():
    text = "```\n@@ -1,2 +1,3 @@\n+x = 1\n```\n\nAdded a line."
    blocks, spoken = sanitize_persona_reply(text, diff_context="")
    assert not any(b.kind == "code" for b in blocks)
    assert "Added a line" in spoken


def test_never_strips_to_nothing():
    """The safety rule that makes stripping safe to do automatically at
    all. A reply that is ENTIRELY an announcement, or entirely a diff dump,
    must fall back to showing the original rather than an empty bubble."""
    only_announcement = "Sure, I'll present this hunk."
    blocks, spoken = sanitize_persona_reply(only_announcement, diff_context="")
    assert spoken.strip(), "must not collapse to empty text"

    only_dump = "```diff\n@@ -1,2 +1,3 @@\n+x = 1\n```"
    blocks, spoken = sanitize_persona_reply(only_dump, diff_context="")
    assert spoken.strip(), "must not collapse to empty text"
    assert blocks, "must not collapse to zero blocks — nothing left to render"


def test_the_original_full_text_is_never_the_callers_job_to_touch():
    """sanitize_persona_reply returns blocks/spoken only — it must not
    mutate or need the caller to alter the raw text passed in, since
    llm_capture.py joins a capture file to a WS frame by exact response
    text and the caller still sends that text unchanged."""
    text = "Sure, I'll present this hunk.\n\nThe real content."
    before = text
    sanitize_persona_reply(text, diff_context="")
    assert text == before


def test_second_paragraph_announcement_shaped_text_is_left_alone():
    """Only the FIRST block is ever treated as a possible announcement — a
    later paragraph that happens to start with "Sure," (mid-reply
    agreement, say) is real content, not a preamble to strip."""
    text = "Here's the change.\n\nSure, I'll admit that's a fair concern."
    _blocks, spoken = sanitize_persona_reply(text, diff_context="")
    assert "Sure, I'll admit" in spoken


# --- corpus-backed: real recorded output, not hand-picked examples -------


def test_corpus_flagged_announcements_are_all_caught(corpus):
    """Cross-checks against qa_agent's independent style checker: every
    narration IT flags as a canned preamble / announcement / opener /
    label must also be caught here, on the first block. The two modules
    are allowed to differ on fence/@@ handling (this one uses the more
    precise diff-comparison check instead), so only the announcement-shape
    flags are compared."""
    from qa_agent.scenarios.scenario_helpers import style_violations

    announcement_flags = {
        "canned preamble",
        "announces the act instead of doing it",
        "output-announcing opener",
        "speaker label",
    }
    checked = 0
    for entry in corpus:
        text = entry["narration_text"]
        if not (set(style_violations(text)) & announcement_flags):
            continue
        checked += 1
        blocks = parse_markdown_blocks(text)
        assert blocks and blocks[0].kind == "paragraph", text
        assert _is_announcement(blocks[0].spoken_text), f"missed: {text!r}"
    if checked == 0:
        pytest.skip("no announcement-shaped entries in this corpus run")


def test_corpus_clean_narrations_are_never_stripped(corpus):
    """The other half of the cross-check: a narration qa_agent's own
    checker found no style violation in must survive with every block
    intact — the false-positive direction, which is the one that would
    actually hurt a real reviewer."""
    from qa_agent.scenarios.scenario_helpers import style_violations

    checked = 0
    for entry in corpus:
        text = entry["narration_text"]
        if style_violations(text):
            continue
        checked += 1
        blocks, _ = sanitize_persona_reply(text, entry.get("diff", ""))
        assert len(blocks) == len(parse_markdown_blocks(text)), f"over-stripped a clean narration: {text!r}"
    assert checked > 0, "corpus had nothing clean to check against — is it stale?"
