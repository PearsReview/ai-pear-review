"""app/web/context.py's change_shape_block — telling the model where a hunk sits in the
wider change.

The cheapest context in the app: no skill, no cache file, no LLM call,
just what the session already has in memory. Without it the model sees one
hunk and reasons as though it were the entire change, which is how one leg
of a nine-file rename gets explained as a standalone edit.

The risk it carries is the mirror of the call map's: naming files the
model cannot read invites it to invent relationships between them. So the
tests below care as much about what the block refuses to say as about what
it says.
"""

from __future__ import annotations

import pytest

from app.services.diff_service import Hunk
from app.web.context import build_project_context, change_shape_block


def _hunk(index: int, file_path: str) -> Hunk:
    return Hunk(index=index, file_path=file_path, header="@@ -1,2 +1,3 @@", lines=["@@ -1,2 +1,3 @@", "+added"])


def test_a_single_hunk_change_has_no_shape_worth_describing():
    hunks = [_hunk(0, "billing.py")]
    assert change_shape_block(hunks, hunks[0]) is None


def test_position_within_the_change_is_reported():
    hunks = [_hunk(i, "billing.py") for i in range(5)]
    block = change_shape_block(hunks, hunks[2])
    assert "hunk 3 of 5" in block


def test_a_single_file_change_says_so_rather_than_listing_nothing():
    hunks = [_hunk(i, "billing.py") for i in range(3)]
    block = change_shape_block(hunks, hunks[0])
    assert "all of it in billing.py" in block
    assert "also touches" not in block


def test_other_touched_files_are_named():
    hunks = [_hunk(0, "billing.py"), _hunk(1, "flow.py"), _hunk(2, "refunds.py")]
    block = change_shape_block(hunks, hunks[0])
    assert "spans 3 files" in block
    assert "flow.py" in block
    assert "refunds.py" in block
    assert "billing.py, it also touches" in block


def test_the_hunks_own_file_is_not_listed_as_an_other_file():
    hunks = [_hunk(0, "billing.py"), _hunk(1, "billing.py"), _hunk(2, "flow.py")]
    block = change_shape_block(hunks, hunks[0])
    assert "it also touches: flow.py" in block


def test_files_are_deduplicated_not_counted_per_hunk():
    """Four hunks across two files is a two-file change. Counting hunks
    here would tell the model a small change is a sprawling one."""
    hunks = [_hunk(0, "a.py"), _hunk(1, "a.py"), _hunk(2, "b.py"), _hunk(3, "b.py")]
    block = change_shape_block(hunks, hunks[0])
    assert "spans 2 files" in block
    assert "hunk 1 of 4" in block


def test_a_sprawling_change_is_summarised_not_enumerated():
    """A 40-file refactor must not spend the diff's budget listing paths."""
    hunks = [_hunk(i, f"module_{i}.py") for i in range(40)]
    block = change_shape_block(hunks, hunks[0])
    assert "and 33 more" in block
    assert len(block) < 500, f"block should stay small, got {len(block)} chars"


def test_the_model_is_told_not_to_speculate_about_files_it_cannot_see():
    """The whole risk of naming unseen files. Without this line the model
    will happily explain how flow.py relates to billing.py on no evidence."""
    hunks = [_hunk(0, "billing.py"), _hunk(1, "flow.py")]
    block = change_shape_block(hunks, hunks[0])
    assert "cannot see those files" in block
    assert "do not guess" in block


@pytest.mark.parametrize("index", [0, 1, 2])
def test_shape_reaches_the_assembled_prompt_with_no_skills_run(index: int, tmp_path):
    """Unlike the overview and call map, this needs no prep skill — so on a
    repo where nobody has run anything, it must still be the one piece of
    project context the model gets."""
    hunks = [_hunk(0, "billing.py"), _hunk(1, "flow.py"), _hunk(2, "refunds.py")]
    session = type("S", (), {"repo_path": str(tmp_path), "hunks": hunks})()

    context = build_project_context(session, hunks[index])
    assert context is not None
    assert f"hunk {index + 1} of 3" in context
    assert "About this project" not in context, "no overview was written for this repo"
