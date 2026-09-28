"""Stage 2 — semantic judges. Unlike every other file in this suite,
these tests always pass: they drive a scenario (same Playwright
mechanics as Stage 1), capture what the app's own LLM produced, and hand
it to a second, isolated LLM session (llm_client.py) for a verdict —
recorded to findings.jsonl (see findings_log.py), never asserted on
directly. Stage 4's aggregator is where "is this actually worth caring
about" gets decided, not pytest's pass/fail. See the plan this was built
from for the full reasoning.
"""

from __future__ import annotations

import difflib
import time

import pytest

from .judge_prompts import (
    ACT_NOW_JUDGE_SYSTEM,
    ACT_NOW_JUDGE_USER,
    NARRATION_JUDGE_SYSTEM,
    NARRATION_JUDGE_USER,
    REPLY_JUDGE_SYSTEM,
    REPLY_JUDGE_USER,
)
from .judging import judge_and_record
from .llm_client import LLMClient

# See test_act_now.py's matching constant for why this is one call's budget
# (60s in app/config.yaml) rather than several: abandoned calls are actually
# cancelled now, so nothing queues ahead of this one that isn't still wanted.
ACT_NOW_TIMEOUT_MS = 45_000

# A hunk's first narration is two chained model calls, not one — a fresh
# briefing (no .briefing/ cache entry yet) and then the narration itself —
# measured live on this machine at up to ~40s combined. This used to be
# 30_000 and did eventually time out here for real, exactly as
# scenarios/test_messy_navigation.py's own copy of this constant predicted
# it would. Duplicated rather than shared for the same reason
# ACT_NOW_TIMEOUT_MS is duplicated between here and test_act_now.py: one
# small number with a local explanation beats an import across the suite.
FRESH_NARRATION_TIMEOUT_MS = 60_000


def test_narration_makes_sense(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    narration = frames.wait_for("narration", page, timeout_ms=FRESH_NARRATION_TIMEOUT_MS)

    judge_and_record(
        category="narration",
        client=LLMClient(**judge_model_config),
        system_prompt=NARRATION_JUDGE_SYSTEM,
        user_prompt=NARRATION_JUDGE_USER.format(
            file_path=presenting["file_path"],
            diff=presenting["diff"],
            narration_text=narration["text"],
        ),
        context={
            "file_path": presenting["file_path"],
            "diff": presenting["diff"],
            "narration_text": narration["text"],
        },
        response_text=narration["text"],
        capture_dir=llm_capture_dir,
    )


def test_reply_makes_sense(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    # Wait for narration before asking. The per-hunk conversation history
    # is empty until the narration turn lands, and the diff only ever
    # enters that history via narration's own prompt (_hunk_prompt in
    # app/services/conversation_service.py) — the reply call itself sends
    # just the question. Asking first therefore judges a model that was
    # handed the bare string "Why was this change made?" and nothing else,
    # which this judge's rubric ("consistent with the diff shown") can only
    # ever mark "no": it was never shown the diff.
    #
    # Found via the llm_call capture this test now records — the finding
    # showed messages == [{"role": "user", "content": "Why was this change
    # made?"}] against a reply confidently describing database-query
    # optimisation in a diff that only adds a farewell() function. Worth
    # knowing that the *app* allows this too (the composer is enabled
    # before narration lands, by design — see setComposerEnabled), so a
    # real reviewer who types quickly gets the same context-free answer;
    # that's an app-side question, not something this test should paper
    # over by pretending the race doesn't exist.
    frames.wait_for("narration", page, timeout_ms=FRESH_NARRATION_TIMEOUT_MS)

    question = "Why was this change made?"
    page.fill("#text-input", question)
    page.click("#send-btn")
    reviewer_turn = frames.wait_for("reviewer_turn", page, timeout_ms=30_000)

    judge_and_record(
        category="reply",
        client=LLMClient(**judge_model_config),
        system_prompt=REPLY_JUDGE_SYSTEM,
        user_prompt=REPLY_JUDGE_USER.format(
            file_path=presenting["file_path"],
            diff=presenting["diff"],
            human_text=question,
            reply_text=reviewer_turn["text"],
        ),
        context={
            "file_path": presenting["file_path"],
            "diff": presenting["diff"],
            "human_text": question,
            "reply_text": reviewer_turn["text"],
        },
        response_text=reviewer_turn["text"],
        capture_dir=llm_capture_dir,
    )


def _full_lines_to_content(full_lines: list[dict]) -> str:
    """Reconstructs the proposed file's text from an act_now_preview
    payload's full_lines (same {kind, old_lineno, new_lineno, text}
    shape Stage 1 already consumes) — every line except the ones marked
    "del" is part of the resulting file, in order."""
    return "\n".join(line["text"] for line in full_lines if line["kind"] != "del")


def _wait_for_act_now_result(frames, page, timeout_ms: int = ACT_NOW_TIMEOUT_MS) -> tuple[str, dict]:
    """Returns (msg_type, payload) for whichever of "act_now_preview" /
    "error" arrives first. act_now_service.generate_change() is now
    honestly probabilistic on this app's default small local model (see
    its module docstring) — a safe rejection when it can't verify a clean
    edit is a correct, expected outcome some of the time, not a bug, so
    this needs to distinguish "the app is broken" from "the model didn't
    produce a safe edit this attempt" rather than only ever waiting for
    success and timing out on what's now a legitimate outcome. start_index
    scopes the search to frames that arrive *after* this call, so a retry
    doesn't immediately re-match a previous attempt's stale error."""
    start_index = len(frames.frames)
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in frames.frames[start_index:]:
            if frame.get("type") in ("act_now_preview", "error"):
                return frame["type"], frame["payload"]
        page.wait_for_timeout(100)
    raise TimeoutError(f"neither 'act_now_preview' nor 'error' received within {timeout_ms}ms")


def _assert_only_target_region_changed(original: str, new: str) -> None:
    """act_now_service.py's search/replace apply now GUARANTEES this
    structurally, before a preview is ever shown (see its module
    docstring on unique-match + whole-line-boundary checks) — asserting it
    here is a cheap, deterministic regression guard, strictly stronger
    than an LLM judge's opinion on the same question. qa_agent's own
    findings.jsonl once recorded the LLM judge unanimously rubber-stamping
    a broken edit (a deleted blank line, a misplaced comment) as
    "without modifying any other part of the code" — this can't have that
    failure mode, since it's exact and has no judgment to sway."""
    matcher = difflib.SequenceMatcher(a=original.splitlines(), b=new.splitlines())
    changed_groups = [op for op in matcher.get_opcodes() if op[0] != "equal"]
    assert len(changed_groups) <= 1, (
        f"expected at most one contiguous changed region outside the target edit, "
        f"found {len(changed_groups)}: {changed_groups}"
    )


@pytest.mark.skip(
    reason=(
        "Act Now now runs through a coding agent (harness_service.py); this suite's app copy uses a "
        "scripted fake agent, whose edit there's nothing to judge. Judging a real agent's edits needs "
        "Cline installed and belongs in qa_agent/live/."
    )
)
def test_act_now_change_makes_sense(
    page_with_ws, scratch_repo, reset_scratch_repo, judge_model_config, llm_capture_dir
):
    page, frames = page_with_ws
    original_content = (scratch_repo / "sample.py").read_text(encoding="utf-8")

    instruction = "Add a one-line comment above the farewell function."

    # Retry a few times before treating "no preview ever arrived" as a
    # real failure — measured live at roughly 8/10 success against this
    # app's default model, so a single rejected attempt is expected
    # variance, not a regression.
    #
    # #act-now-btn is re-clicked every attempt, not once up front: Act Now
    # is one-shot by design and sendTextReply() deactivates it on send (see
    # static/js/interactions.js and test_act_now.py's _submit_act_now, which documents
    # this at length). Clicking once meant every retry went out as an
    # ordinary chat reply and then waited out the full timeout for a frame
    # that was never coming — half of code review P1's "hangs with nothing
    # logged server-side".
    preview = None
    last_error = None
    for _attempt in range(4):
        page.click("#act-now-btn")
        page.fill("#text-input", instruction)
        page.click("#send-btn")
        msg_type, payload = _wait_for_act_now_result(frames, page)
        if msg_type == "act_now_preview":
            preview = payload
            break
        last_error = payload.get("message")
    assert preview is not None, f"Act Now was safely rejected on every attempt (last error: {last_error!r})"

    new_content = _full_lines_to_content(preview["full_lines"])
    _assert_only_target_region_changed(original_content, new_content)

    # No response_text/capture_dir here: what's judged is the *applied
    # file*, reconstructed from the preview's full_lines, not a single
    # model response string — there's no verbatim text to join a capture
    # on (the model emitted a {"search","replace"} object, not this file).
    judge_and_record(
        category="act_now",
        client=LLMClient(**judge_model_config),
        system_prompt=ACT_NOW_JUDGE_SYSTEM,
        user_prompt=ACT_NOW_JUDGE_USER.format(
            original_content=original_content, instruction=instruction, new_content=new_content
        ),
        context={"instruction": instruction, "original_content": original_content, "new_content": new_content},
    )
