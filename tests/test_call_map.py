"""app/services/call_map.py — reading the skill-written call map and
slicing out the callers relevant to one hunk.

The properties under test are mostly about restraint. A call map is
injected as background fact, so the ways it can hurt are: claiming
something is uncalled when the scanner merely missed it, spending the
diff's token budget on caller lists, and surviving a malformed file badly.
Everything below is one of those three.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.call_map import (
    call_map_path,
    call_map_prompt_block,
    call_map_status,
    load_call_map,
)

_MAP = {
    "schema_version": 1,
    "generated_at": "2026-09-02T10:00:00Z",
    "head_sha": "abc123",
    "language": "python",
    "symbols": [
        {
            "name": "charge",
            "file": "billing.py",
            "kind": "function",
            "line": 10,
            "callers": [
                {"name": "checkout", "file": "flow.py"},
                {"name": "refund", "file": "refunds.py"},
            ],
            "caller_count": 2,
        },
        {
            "name": "validate_amount",
            "file": "billing.py",
            "kind": "function",
            "line": 30,
            "callers": [{"name": "charge", "file": "billing.py"}],
            "caller_count": 1,
        },
        {
            "name": "render",
            "file": "views.py",
            "kind": "function",
            "line": 5,
            "callers": [{"name": "handle", "file": "flow.py"}],
            "caller_count": 1,
        },
    ],
}

_CHARGE_HUNK = (
    "@@ -8,3 +8,5 @@\n def charge(amount):\n+    if amount < 0:\n+        raise ValueError\n     return amount"
)


def _write(repo: Path, data: dict) -> None:
    path = call_map_path(str(repo))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_missing_call_map_is_silent(tmp_path: Path):
    assert load_call_map(str(tmp_path)) is None
    assert call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK) is None
    assert call_map_status(str(tmp_path), "abc123") == {"present": False}


@pytest.mark.parametrize(
    "content",
    [
        "not json at all {{{",
        json.dumps(["a", "list"]),
        json.dumps({"symbols": []}),  # scanned, found nothing
        json.dumps({"symbols": "not a list"}),
        json.dumps({}),
    ],
)
def test_unusable_maps_are_ignored_rather_than_injected(tmp_path: Path, content: str):
    path = call_map_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    assert load_call_map(str(tmp_path)) is None
    assert call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK) is None


def test_callers_of_the_touched_symbol_are_injected(tmp_path: Path):
    _write(tmp_path, _MAP)
    block = call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK)
    assert "charge is called by:" in block
    assert "checkout (flow.py)" in block
    assert "refund (refunds.py)" in block


def test_symbols_the_hunk_does_not_touch_are_left_out(tmp_path: Path):
    """The map is repo-wide; the prompt budget is per-hunk. Only the
    symbols actually in front of the reviewer earn their space."""
    _write(tmp_path, _MAP)
    block = call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK)
    assert "validate_amount" not in block


def test_symbols_defined_in_other_files_are_not_claimed(tmp_path: Path):
    """`render` is defined in views.py. A hunk in billing.py that happens
    to mention the word must not pick up views.py's callers — that would
    attribute one file's dependencies to another."""
    _write(tmp_path, _MAP)
    hunk = "@@ -1,2 +1,3 @@\n def charge(amount):\n+    render(amount)\n"
    block = call_map_prompt_block(str(tmp_path), "billing.py", hunk)
    assert "render is called by" not in block
    assert "handle" not in block


def test_context_lines_count_as_touching(tmp_path: Path):
    """A hunk editing the middle of a function body won't repeat the def
    line as a changed line — it shows up as unchanged context. That's
    exactly when callers matter, so context lines must count."""
    _write(tmp_path, _MAP)
    hunk = (
        "@@ -10,4 +10,4 @@\n def charge(amount):\n     total = amount\n-    return total\n+    return round(total, 2)"
    )
    block = call_map_prompt_block(str(tmp_path), "billing.py", hunk)
    assert "charge is called by:" in block


def test_partial_word_matches_do_not_count(tmp_path: Path):
    """`recharge_battery` is not `charge`. Substring matching would attach
    a payment function's callers to an unrelated hunk."""
    _write(tmp_path, _MAP)
    hunk = "@@ -1,2 +1,3 @@\n def recharge_battery(x):\n+    charged = True\n     return x"
    assert call_map_prompt_block(str(tmp_path), "billing.py", hunk) is None


def test_no_callers_produces_silence_not_a_claim_of_being_unused(tmp_path: Path):
    """The safety property this module exists to hold. An empty caller
    list means the scanner found nothing — dynamic dispatch, a config-wired
    entry point, or a gap in the scan all look like this. Saying "nothing
    calls this" would be a confident falsehood a reviewer might delete
    live code over."""
    _write(
        tmp_path,
        {**_MAP, "symbols": [{"name": "charge", "file": "billing.py", "callers": [], "caller_count": 0}]},
    )
    block = call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK)
    assert block is None, "a symbol with no known callers must produce no text at all"


def test_recursion_is_not_reported_as_a_dependency(tmp_path: Path):
    _write(
        tmp_path,
        {
            **_MAP,
            "symbols": [
                {
                    "name": "charge",
                    "file": "billing.py",
                    "callers": [
                        {"name": "charge", "file": "billing.py"},
                        {"name": "checkout", "file": "flow.py"},
                    ],
                    "caller_count": 2,
                }
            ],
        },
    )
    block = call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK)
    assert "checkout (flow.py)" in block
    assert "charge (billing.py)" not in block


def test_duplicate_callers_are_collapsed(tmp_path: Path):
    _write(
        tmp_path,
        {
            **_MAP,
            "symbols": [
                {
                    "name": "charge",
                    "file": "billing.py",
                    "callers": [
                        {"name": "checkout", "file": "flow.py"},
                        {"name": "checkout", "file": "flow.py"},
                    ],
                    "caller_count": 2,
                }
            ],
        },
    )
    block = call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK)
    assert block.count("checkout (flow.py)") == 1


def test_a_huge_caller_list_cannot_eat_the_diffs_budget(tmp_path: Path):
    """Scanner-written text the app doesn't control. A function called from
    400 places must not push the diff out of the context window — the diff
    is the thing being reviewed."""
    _write(
        tmp_path,
        {
            **_MAP,
            "symbols": [
                {
                    "name": "charge",
                    "file": "billing.py",
                    "callers": [{"name": f"caller_{i}", "file": f"mod_{i}.py"} for i in range(400)],
                    "caller_count": 400,
                }
            ],
        },
    )
    block = call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK)
    assert len(block) < 900, f"block should stay small, got {len(block)} chars"
    assert "and 395 more" in block, "the count that was elided should still be visible"


def test_many_touched_symbols_are_capped(tmp_path: Path):
    symbols = [
        {
            "name": f"fn_{i}",
            "file": "billing.py",
            "callers": [{"name": f"c_{i}", "file": "flow.py"}],
            "caller_count": 1,
        }
        for i in range(30)
    ]
    _write(tmp_path, {**_MAP, "symbols": symbols})
    hunk = "\n".join(f"+    fn_{i}()" for i in range(30))
    block = call_map_prompt_block(str(tmp_path), "billing.py", hunk)
    assert block.count(" is called by:") <= 6


def test_windows_style_paths_match_posix_entries(tmp_path: Path):
    _write(
        tmp_path,
        {
            **_MAP,
            "symbols": [
                {
                    "name": "charge",
                    "file": "app/billing.py",
                    "callers": [{"name": "checkout", "file": "flow.py"}],
                    "caller_count": 1,
                },
            ],
        },
    )
    block = call_map_prompt_block(str(tmp_path), "app\\billing.py", _CHARGE_HUNK)
    assert "charge is called by:" in block


def test_regex_metacharacters_in_a_name_do_not_raise(tmp_path: Path):
    """The map is written by a script the app doesn't control. A junk name
    should fail to match, not blow up inside a narration."""
    _write(
        tmp_path,
        {
            **_MAP,
            "symbols": [
                {
                    "name": "a(b|c)+",
                    "file": "billing.py",
                    "callers": [{"name": "x", "file": "y.py"}],
                    "caller_count": 1,
                },
            ],
        },
    )
    assert call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK) is None


def test_malformed_symbol_entries_are_skipped_not_fatal(tmp_path: Path):
    _write(
        tmp_path,
        {
            **_MAP,
            "symbols": [
                "not a dict",
                {"no_name": True},
                {"name": "charge", "file": "billing.py", "callers": "not a list"},
                {
                    "name": "charge",
                    "file": "billing.py",
                    "callers": [{"name": "checkout", "file": "flow.py"}],
                    "caller_count": 1,
                },
            ],
        },
    )
    block = call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK)
    assert "checkout (flow.py)" in block


def test_status_reports_head_drift_without_discarding(tmp_path: Path):
    _write(tmp_path, _MAP)
    status = call_map_status(str(tmp_path), "def456")
    assert status["present"] is True
    assert status["head_moved"] is True
    assert status["symbol_count"] == 3
    assert call_map_prompt_block(str(tmp_path), "billing.py", _CHARGE_HUNK) is not None

    assert call_map_status(str(tmp_path), "abc123")["head_moved"] is False


def test_build_project_context_leaves_out_the_call_map(tmp_path: Path):
    """The call map is switched off for narration (Python only; callers are
    to come from the coding agent), even when the repo has one on disk."""
    from app.services.diff_service import Hunk
    from app.services.project_overview import overview_path
    from app.web.context import build_project_context

    overview_path(str(tmp_path)).parent.mkdir(parents=True, exist_ok=True)
    overview_path(str(tmp_path)).write_text(
        json.dumps({"digest": "A payments service.", "components": []}), encoding="utf-8"
    )
    _write(tmp_path, _MAP)

    hunk = Hunk(index=0, file_path="billing.py", header="@@ -8,3 +8,5 @@", lines=_CHARGE_HUNK.splitlines())
    session = type("S", (), {"repo_path": str(tmp_path), "hunks": [hunk]})()

    context = build_project_context(session, hunk)
    assert "A payments service." in context
    assert "Who calls the code" not in context
    assert "checkout (flow.py)" not in context


def test_build_project_context_is_none_when_no_skill_has_run(tmp_path: Path):
    from app.services.diff_service import Hunk
    from app.web.context import build_project_context

    # A single hunk, so change_shape_block stays silent too and the whole
    # context really is empty — the pre-skill behaviour.
    hunk = Hunk(index=0, file_path="billing.py", header="@@ -1,1 +1,1 @@", lines=["+x"])
    session = type("S", (), {"repo_path": str(tmp_path), "hunks": [hunk]})()
    assert build_project_context(session, hunk) is None


# --- test coverage (used when the reviewer asks whether something is tested) ---

from app.services.call_map import coverage_prompt_block as coverage_block  # noqa: E402


def _coverage_map(**charge_fields) -> dict:
    charge = {"name": "charge", "file": "billing.py", "callers": [{"name": "checkout", "file": "flow.py"}]}
    charge.update(charge_fields)
    return {"head_sha": "abc", "symbols": [charge]}


def test_coverage_names_the_test_files(tmp_path: Path):
    _write(tmp_path, _coverage_map(test_caller_count=2, test_files=["tests/test_billing.py"]))
    block = coverage_block(str(tmp_path), "billing.py", "+    return charge(x)")
    assert "charge is called directly by 2 test call site(s) (tests/test_billing.py)" in block
    assert "indirectly" in block


def test_no_test_callers_is_stated_narrowly(tmp_path: Path):
    """Never "untested": a browser suite covers code without calling it by name."""
    _write(tmp_path, _coverage_map(test_caller_count=0, test_files=[]))
    block = coverage_block(str(tmp_path), "billing.py", "+    return charge(x)")
    assert "no direct test calls to charge were found" in block
    assert "untested" not in block.lower()


def test_a_map_without_coverage_fields_says_nothing(tmp_path: Path):
    """A map written by the older scanner: a missing count is not a zero."""
    _write(tmp_path, _coverage_map())
    assert coverage_block(str(tmp_path), "billing.py", "+    return charge(x)") is None


def test_coverage_only_for_symbols_the_hunk_touches(tmp_path: Path):
    _write(tmp_path, _coverage_map(test_caller_count=2, test_files=["tests/test_billing.py"]))
    assert coverage_block(str(tmp_path), "billing.py", "+    unrelated = 1") is None
    assert coverage_block(str(tmp_path), "other.py", "+    return charge(x)") is None
