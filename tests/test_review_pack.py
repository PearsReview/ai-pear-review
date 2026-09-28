"""qa_agent/generated/review_pack.py — the deterministic flags.

`foreign_symbols` is load-bearing: a test in the generated suite *asserts*
on it, so a false positive fails the suite on a perfectly good narration
and would train everyone to ignore it. These tests are mostly about what it
must NOT flag.

No browser, no server, no Ollama — pure functions over strings, so they
live with the fast unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from qa_agent.generated.review_pack import foreign_symbols, near_duplicates


@dataclass
class _File:
    path: str
    symbols: list[str]


@dataclass
class _Spec:
    files: list[_File] = field(default_factory=list)


SPEC = _Spec(
    files=[
        _File("billing.py", ["charge_item", "validate_amount", "_describe_step_0"]),
        _File("flow.py", ["checkout_cart", "_describe_step_0"]),
        _File("stock/reorder.py", ["reserve_pallet"]),
    ]
)

DIFF = "@@ -1,3 +1,5 @@\n def charge_item(item):\n+    if item is None:\n+        raise ValueError\n     return item"


def test_a_symbol_from_another_file_is_flagged():
    """The failure this exists to catch: describing the hunk in terms of a
    function that lives somewhere else entirely."""
    narration = "This adds a guard to checkout_cart before it processes the order."
    flagged = foreign_symbols(narration, SPEC, "billing.py", DIFF)
    assert any("checkout_cart" in f for f in flagged)
    assert "flow.py" in flagged[0], "the flag should say where the symbol actually lives"


def test_the_hunks_own_symbols_are_never_flagged():
    narration = "charge_item now rejects a missing item, and validate_amount is unchanged."
    assert foreign_symbols(narration, SPEC, "billing.py", DIFF) == []


def test_a_symbol_visible_in_the_diff_is_never_flagged():
    """If the model could read it, naming it is legitimate — regardless of
    which file the generator says owns it."""
    diff_mentioning_other = DIFF + "\n+    return checkout_cart(item)"
    assert foreign_symbols("It now calls checkout_cart.", SPEC, "billing.py", diff_mentioning_other) == []


def test_symbols_defined_in_several_files_are_never_flagged():
    """Filler functions appear in most generated modules. A name with more
    than one home cannot be attributed, so it must not be evidence of
    anything — this is the main false-positive guard."""
    assert foreign_symbols("The _describe_step_0 helper is untouched.", SPEC, "billing.py", DIFF) == []


def test_ordinary_english_is_not_mistaken_for_a_symbol():
    narration = "This change adds a guard so the function rejects invalid input early."
    assert foreign_symbols(narration, SPEC, "billing.py", DIFF) == []


def test_substring_matches_do_not_count():
    """`recharge_item_total` contains `charge_item`, but naming it is not
    naming `charge_item` — identifier extraction has to be whole-token."""
    assert foreign_symbols("The recharge_item_total path is unaffected.", SPEC, "billing.py", DIFF) == []


def test_a_symbol_from_a_nested_folder_is_still_attributed_correctly():
    flagged = foreign_symbols("It calls reserve_pallet first.", SPEC, "billing.py", DIFF)
    assert flagged and "stock/reorder.py" in flagged[0]


def test_flags_are_sorted_and_deduplicated():
    narration = "checkout_cart and checkout_cart again, plus reserve_pallet."
    flagged = foreign_symbols(narration, SPEC, "billing.py", DIFF)
    assert len(flagged) == 2
    assert flagged == sorted(flagged)


def test_empty_narration_flags_nothing():
    assert foreign_symbols("", SPEC, "billing.py", DIFF) == []


def _entry(path: str, text: str) -> dict:
    return {"file_path": path, "narration_text": text}


def test_near_duplicates_spots_near_identical_narrations():
    entries = [
        _entry("a.py", "This adds a guard that rejects a missing value before continuing."),
        _entry("b.py", "This adds a guard that rejects a missing value before proceeding."),
    ]
    pairs = near_duplicates(entries)
    assert len(pairs) == 1
    assert pairs[0]["ratio"] >= 0.85


def test_genuinely_different_narrations_are_not_paired():
    entries = [
        _entry("a.py", "A guard now rejects negative amounts before the charge is made."),
        _entry("b.py", "The return value is clamped so callers never see a zero total."),
    ]
    assert near_duplicates(entries) == []


def test_near_duplicates_is_empty_for_a_single_hunk():
    assert near_duplicates([_entry("a.py", "Anything at all.")]) == []


@pytest.mark.parametrize("count", [3, 4])
def test_every_pair_is_considered(count: int):
    entries = [_entry(f"f{i}.py", "The same sentence, exactly.") for i in range(count)]
    expected_pairs = count * (count - 1) // 2
    assert len(near_duplicates(entries)) == expected_pairs
