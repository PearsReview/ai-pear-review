"""What of a briefing reaches the narration prompt, and what deliberately
doesn't.

The briefing agent runs first and the narration agent is handed its notes,
so anything injected here is repeated to the reviewer as established fact —
often verbatim. That makes the choice of which fields cross the boundary a
correctness question, not a formatting one, and worth pinning.
"""

from __future__ import annotations

import pytest

from app.services.briefing_service import Briefing
from app.services.conversation_service import ConversationClient
from app.services.diff_service import Hunk


@pytest.fixture
def client() -> ConversationClient:
    # Prompt assembly needs no config and no network.
    return ConversationClient.__new__(ConversationClient)


@pytest.fixture
def hunk() -> Hunk:
    return Hunk(
        index=0,
        file_path="stock/barcode.py",
        header="@@ -37,4 +39,4 @@",
        lines=["@@ -37,4 +39,4 @@", " def count_sku(sku):", "-    return total", "+    return max(total, 1)"],
    )


def _briefing(**overrides) -> Briefing:
    data = {
        "intent": "Ensure the SKU count is always at least 1.",
        "alternatives_considered": None,
        # A real recorded fabrication, kept verbatim as the test fixture.
        "risk_notes": "This might affect performance, as max() could have a higher time complexity.",
        "confidence": "high",
    }
    data.update(overrides)
    return Briefing(**data)


def test_intent_reaches_the_narration_prompt(client, hunk):
    prompt = client._hunk_prompt(hunk, _briefing())
    assert "Ensure the SKU count is always at least 1." in prompt


def test_alternatives_reach_the_narration_prompt(client, hunk):
    prompt = client._hunk_prompt(hunk, _briefing(alternatives_considered="Clamping in the caller."))
    assert "Clamping in the caller." in prompt


def test_a_locally_generated_risk_note_never_reaches_the_prompt(client, hunk):
    """The field this suite measured doing real harm.

    Whatever is injected is repeated to the reviewer as fact about their
    own code, so the bar is whether the author could have known. A 7B model
    given one hunk could not: the example below is real — `max(total, 1)`
    has no size-dependent cost, and a reviewer was told it might.
    """
    prompt = client._hunk_prompt(hunk, _briefing(source="generated"))
    assert "higher time complexity" not in prompt
    assert "Anything you're not fully sure about" not in prompt


def test_an_unlabelled_briefing_is_treated_as_locally_generated(client, hunk):
    """Cache files written before `source` existed have no label. Trusting
    an unlabelled note would silently re-admit exactly what was removed, so
    the default has to be the untrusting one."""
    prompt = client._hunk_prompt(hunk, _briefing())  # source defaults
    assert "higher time complexity" not in prompt


def test_a_skill_written_risk_note_does_reach_the_prompt(client, hunk):
    """The other half, and the reason this is a source gate rather than a
    deletion. The prep-review skill's notes are written by a capable model
    with the repo, git history and tools — a different proposition
    entirely, and the one case where the field earns its place."""
    prompt = client._hunk_prompt(
        hunk,
        _briefing(
            source="prep-review-skill",
            risk_notes="Three callers in checkout.py pass a bare int and will now raise.",
        ),
    )
    assert "Three callers in checkout.py" in prompt
    assert "Anything you're not fully sure about" in prompt


def test_confidence_does_not_launder_a_locally_generated_risk_note(client, hunk):
    """`confidence` is documented as being about whether real intent was
    found, but it gates the whole object — so "high" must not become a
    reason to trust an unrelated field."""
    prompt = client._hunk_prompt(hunk, _briefing(confidence="high", source="generated"))
    assert "higher time complexity" not in prompt


def test_a_low_confidence_briefing_contributes_nothing(client, hunk):
    prompt = client._hunk_prompt(hunk, _briefing(confidence="low"))
    assert "Ensure the SKU count" not in prompt
    assert "What you actually know" not in prompt


def test_no_briefing_leaves_the_prompt_as_the_bare_diff(client, hunk):
    prompt = client._hunk_prompt(hunk, None)
    assert "What you actually know" not in prompt
    assert "return max(total, 1)" in prompt


def test_the_diff_is_always_present_regardless_of_briefing(client, hunk):
    """Whatever else changes, the thing being reviewed has to be there."""
    for briefing in (None, _briefing(), _briefing(confidence="low")):
        assert "+    return max(total, 1)" in client._hunk_prompt(hunk, briefing)


# --- summary, theme and related hunks in the normal (diff-visible) prompt ---

from app.services.briefing_service import ChangeContext, RelatedHunk  # noqa: E402

_CONTEXT = ChangeContext(
    "Clamp SKU counts",
    "Zero counts broke the barcode printer queue.",
    (RelatedHunk(4, "stock/printer.py", "caller_of", "print_labels", "Stops dividing by the count."),),
)


def test_summary_stays_out_when_the_diff_speaks_for_itself(client, hunk):
    """Next to a readable diff, a prose summary is only something for a
    small model to repeat back — or believe over the code."""
    prompt = client._hunk_prompt(hunk, _briefing(summary="Clamps the count to at least 1.", kind="behaviour"))
    assert "Clamps the count" not in prompt


def test_summary_goes_in_when_the_diff_misleads(client, hunk):
    """A pure move reads as a deletion; the summary is what says otherwise."""
    for kind in ("move", "mechanical"):
        prompt = client._hunk_prompt(hunk, _briefing(summary="Moved to stock/counts.py.", kind=kind))
        assert "Moved to stock/counts.py." in prompt


def test_theme_and_related_names_reach_the_normal_prompt(client, hunk):
    prompt = client._hunk_prompt(hunk, _briefing(), None, _CONTEXT)
    assert "Zero counts broke the barcode printer queue." in prompt
    assert "caller of stock/printer.py (print_labels)" in prompt
    # Related hunks' own summaries are for when the diff can't be shown.
    assert "Stops dividing by the count." not in prompt


def test_low_confidence_contributes_no_change_context_either(client, hunk):
    prompt = client._hunk_prompt(hunk, _briefing(confidence="low", summary="s", kind="move"), None, _CONTEXT)
    assert "barcode printer queue" not in prompt and "stock/printer.py" not in prompt
