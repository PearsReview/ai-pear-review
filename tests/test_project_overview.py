"""app/services/project_overview.py — reading the skill-written overview
and slicing it down to what's worth putting in a prompt.

The properties that matter here are all about *not* making narration
worse: absence must behave exactly like the pre-overview app, a malformed
or half-written file must not leak into a prompt, and no single entry may
quietly eat a budget that the diff itself needs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.project_overview import (
    load_overview,
    overview_path,
    overview_prompt_block,
    overview_status,
)

_OVERVIEW = {
    "schema_version": 1,
    "generated_at": "2026-09-02T10:00:00Z",
    "head_sha": "abc123",
    "digest": "A local web app that walks a reviewer through uncommitted git changes hunk by hunk.",
    "components": [
        {"path": "app/services", "role": "Service layer: diff reading, LLM clients, caches."},
        {"path": "app/services/diff_service.py", "role": "Splits git diff output into review hunks."},
        {"path": "static", "role": "The browser UI, served as plain files with no build step."},
    ],
}


def _write(repo: Path, data: dict) -> None:
    path = overview_path(str(repo))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_missing_overview_is_silent(tmp_path: Path):
    assert load_overview(str(tmp_path)) is None
    assert overview_prompt_block(str(tmp_path)) is None
    assert overview_status(str(tmp_path), "abc123") == {"present": False}


@pytest.mark.parametrize(
    "content",
    [
        "not json at all {{{",
        json.dumps(["a", "list", "not", "an", "object"]),
        json.dumps({"components": [], "digest": ""}),  # skeleton, never filled in
        json.dumps({"components": []}),  # no digest key at all
    ],
)
def test_unusable_overviews_are_ignored_rather_than_injected(tmp_path: Path, content: str):
    path = overview_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    assert load_overview(str(tmp_path)) is None
    assert overview_prompt_block(str(tmp_path)) is None


def test_block_carries_the_digest(tmp_path: Path):
    _write(tmp_path, _OVERVIEW)
    block = overview_prompt_block(str(tmp_path))
    assert "walks a reviewer through uncommitted git changes" in block
    # The model is told this is background, not material to narrate — the
    # reviewer already knows what their own project is.
    assert "context, not something to describe" in block


def test_exact_file_entry_wins_over_a_directory_prefix(tmp_path: Path):
    _write(tmp_path, _OVERVIEW)
    block = overview_prompt_block(str(tmp_path), "app/services/diff_service.py")
    assert "Splits git diff output into review hunks." in block
    assert "Service layer" not in block


def test_directory_entry_covers_files_beneath_it(tmp_path: Path):
    """The skill can't enumerate every file in a repo, so a directory entry
    has to cover what's under it — otherwise the per-file slice is only
    ever available for files someone thought to list."""
    _write(tmp_path, _OVERVIEW)
    block = overview_prompt_block(str(tmp_path), "app/services/voice_service.py")
    assert "Service layer" in block


def test_longest_matching_prefix_wins(tmp_path: Path):
    _write(
        tmp_path,
        {
            **_OVERVIEW,
            "components": [
                {"path": "app", "role": "The whole backend."},
                {"path": "app/services", "role": "Service layer."},
            ],
        },
    )
    block = overview_prompt_block(str(tmp_path), "app/services/diff_service.py")
    assert "Service layer." in block
    assert "The whole backend." not in block


def test_unknown_file_still_gets_the_digest(tmp_path: Path):
    _write(tmp_path, _OVERVIEW)
    block = overview_prompt_block(str(tmp_path), "tools/unlisted_thing.py")
    assert "walks a reviewer through" in block
    assert "About tools/unlisted_thing.py" not in block


def test_windows_style_paths_match_posix_entries(tmp_path: Path):
    _write(tmp_path, _OVERVIEW)
    block = overview_prompt_block(str(tmp_path), "app\\services\\diff_service.py")
    assert "Splits git diff output into review hunks." in block


def test_oversized_entries_are_truncated(tmp_path: Path):
    """Skill-written text the app doesn't control. One runaway entry must
    not silently consume the budget the diff needs — that would degrade
    every narration in a way nobody would trace back to this file."""
    _write(
        tmp_path,
        {
            **_OVERVIEW,
            "digest": "D" * 50_000,
            "components": [{"path": "app", "role": "R" * 50_000}],
        },
    )
    block = overview_prompt_block(str(tmp_path), "app/server.py")
    assert len(block) < 2_000, f"prompt block should stay small, got {len(block)} chars"


def test_hunk_prompt_carries_project_context_when_given():
    """The other half of the contract: the block this module produces has
    to actually reach the narration prompt, ahead of the diff, and be
    absent entirely when there's no overview."""
    from app.services.conversation_service import ConversationClient
    from app.services.diff_service import Hunk

    client = ConversationClient.__new__(ConversationClient)  # no network/config needed for prompt assembly
    hunk = Hunk(index=0, file_path="app/x.py", header="@@ -1,2 +1,3 @@", lines=["@@ -1,2 +1,3 @@", "+added"])

    with_context = client._hunk_prompt(hunk, None, "About this project: a thing that does stuff.")
    assert "About this project: a thing that does stuff." in with_context
    assert with_context.index("About this project") < with_context.index("Diff hunk:")
    assert "+added" in with_context

    without = client._hunk_prompt(hunk, None, None)
    assert "About this project" not in without
    assert without.startswith("File: app/x.py"), "no overview should leave the prompt exactly as it was before"


def test_conventions_reach_the_prompt(tmp_path: Path):
    """The one part of an overview a reviewer can check a diff *against* —
    a change that breaks a house rule is a finding, where the digest is
    only ever framing. It was recorded but unused until now."""
    _write(
        tmp_path,
        {
            **_OVERVIEW,
            "conventions": [
                "Derived caches live in gitignored dot-dirs at the repo root",
                "The app never invokes the Claude CLI itself",
            ],
        },
    )
    block = overview_prompt_block(str(tmp_path), "app/server.py")
    assert "House rules in this project:" in block
    assert "Derived caches live in gitignored dot-dirs" in block
    assert "never invokes the Claude CLI" in block


def test_an_overlong_convention_is_dropped_whole_not_truncated(tmp_path: Path):
    """A cut-off rule reads as a complete sentence and states something
    false — real example from this repo's own overview: "a helper two
    handlers need moves down into." Same rule question_context.py applies
    to its blocks: half a fact misleads more than none."""
    _write(tmp_path, {**_OVERVIEW, "conventions": [f"Rule number {i}. " + "x" * 500 for i in range(20)]})
    block = overview_prompt_block(str(tmp_path), "app/server.py")
    assert "House rules" not in block, "every rule was over the cap, so none should appear"
    assert "walks a reviewer through" in block, "the rest of the overview must still work"


def test_conventions_are_capped_in_number(tmp_path: Path):
    """Conventions apply to every hunk equally, so they're the first thing
    worth cutting when the diff needs the room — only the few the skill
    ranked highest get through."""
    _write(tmp_path, {**_OVERVIEW, "conventions": [f"Rule number {i}." for i in range(20)]})
    block = overview_prompt_block(str(tmp_path), "app/server.py")
    assert block.count("Rule number") == 3


def test_a_bigger_window_admits_a_longer_convention(tmp_path: Path):
    """The caps were measured against an 8k local window; scale is how a
    provider that manages its own window stops paying them (see
    context_scale in app/web/context.py)."""
    rule = "Rule number 0. " + "x" * 300
    _write(tmp_path, {**_OVERVIEW, "conventions": [rule]})
    assert "House rules" not in overview_prompt_block(str(tmp_path), "app/server.py")
    assert "Rule number 0" in overview_prompt_block(str(tmp_path), "app/server.py", scale=6)


@pytest.mark.parametrize("conventions", [None, [], "not a list", [""], [None, 42]])
def test_missing_or_junk_conventions_are_simply_absent(tmp_path: Path, conventions):
    _write(tmp_path, {**_OVERVIEW, "conventions": conventions})
    block = overview_prompt_block(str(tmp_path), "app/server.py")
    assert "House rules" not in block
    assert "walks a reviewer through" in block, "the rest of the overview must still work"


def test_status_reports_head_drift_without_discarding(tmp_path: Path):
    """An overview describes a project's shape, which doesn't stop being
    true because HEAD moved — unlike a per-hunk briefing bound to exact
    diff bytes. So drift is reported, and the overview still loads."""
    _write(tmp_path, _OVERVIEW)
    status = overview_status(str(tmp_path), "def456")
    assert status["present"] is True
    assert status["head_moved"] is True
    assert status["head_sha"] == "abc123"
    assert overview_prompt_block(str(tmp_path)) is not None

    assert overview_status(str(tmp_path), "abc123")["head_moved"] is False
