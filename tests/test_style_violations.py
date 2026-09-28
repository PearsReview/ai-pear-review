"""qa_agent's deterministic spoken-form rules (scenario_helpers.style_violations).

These patterns gate an assertion in the scenarios suite, so both directions
matter equally: a missed violation lets a real regression through, and a
false positive fails a perfectly good narration and teaches everyone to
ignore the check.

The awkward cases are all variations on "Sure" — which is ordinary speech
in a reply and a canned preamble in a narration, depending entirely on what
follows it. Each case below is either something the suite has actually
recorded or the specific false positive an earlier version of these
patterns produced.
"""

from __future__ import annotations

import pytest

from qa_agent.scenarios.scenario_helpers import style_violations


@pytest.mark.parametrize(
    "text,expected",
    [
        # Recorded by the generated-repo suite: a preamble as a complete
        # sentence, then a blank line, then the real narration. The
        # colon-anchored rule missed it entirely, which is why the
        # "announces the act" pattern exists.
        (
            "Sure, I'll present this hunk.\n\nThe change modifies reserve_sku...",
            "announces the act instead of doing it",
        ),
        ("Certainly, let me walk you through it.", "announces the act instead of doing it"),
        ("Okay, I will explain the change.", "announces the act instead of doing it"),
        ("Ok, I can describe this hunk.", "announces the act instead of doing it"),
        # Recorded earlier in this suite's life — breaks three rules at once.
        ("Sure, here's the hunk: ```@@ -1,2 +1,5 @@", "canned preamble"),
        ("Here's what changed in this hunk.", "output-announcing opener"),
        ("Narration: the function now validates its input.", "speaker label"),
        ("```python\nx = 1\n```", "markdown code fence"),
        ("@@ -1,2 +1,5 @@", "raw diff hunk header"),
    ],
)
def test_real_violations_are_caught(text: str, expected: str):
    assert expected in style_violations(text), f"{expected!r} not flagged in {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        # The false positive an earlier, looser version of the preamble rule
        # produced. "Sure" as agreement is ordinary speech in a reply, and
        # flagging it would make the check useless.
        "Sure, that makes sense to me.",
        "Sure. The function now rejects None values.",
        "Of course the guard is redundant, since callers already check.",
        "Okay, that is a reasonable concern about the return value.",
        # Normal narrations, including ones naming identifiers in backticks —
        # explicitly fine per the app's own prompt.
        "This adds a validation check so the item is never None.",
        "I changed `return total` to `return max(total, 1)` to avoid a zero count.",
        "The `audit_item` function now raises a ValueError for missing input.",
        "",
    ],
)
def test_ordinary_speech_is_never_flagged(text: str):
    assert style_violations(text) == [], f"false positive on {text!r}"
