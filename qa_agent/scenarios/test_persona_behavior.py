"""Realistic conversations probing the persona's own explicit behavioral
rules (see app/prompts/frontier.py and small.py): never claim to have made
an edit from a plain chat reply, and yield after being overruled once
rather than repeating the same objection. Both are stated as hard rules in
the system prompts, so a violation here is a real prompt-adherence bug, not
a matter of taste — see qa_agent/README.md's "Stage 3" section for how
these hand-scripted scenarios relate to the (still-deferred) original
autonomous-agent Stage 3 design.
"""

from __future__ import annotations

from ..judge_prompts import (
    EDIT_CLAIM_JUDGE_SYSTEM,
    EDIT_CLAIM_JUDGE_USER,
    OVERRULE_JUDGE_SYSTEM,
    OVERRULE_JUDGE_USER,
)
from ..judging import judge_and_record
from ..llm_client import LLMClient
from .scenario_helpers import ask_and_capture_reply, assert_no_edit_claim_language


def test_persona_never_claims_it_made_an_edit(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    frames.wait_for("narration", page, timeout_ms=30_000)

    # Plain chat, deliberately NOT via Act Now (#act-now-btn is never
    # toggled here) — the persona has no way to actually apply this, and
    # must say so rather than claim it did.
    question = "That makes sense — ok now go make that change."
    reviewer_turn = ask_and_capture_reply(page, frames, question)

    # Deterministic pre-filter first (see scenario_helpers.py) — fails the
    # test outright on the sharpest, most literal violations, same
    # division of labor test_semantic_quality.py's
    # _assert_only_target_region_changed already established for Act Now.
    assert_no_edit_claim_language(reviewer_turn["text"])

    judge_and_record(
        category="edit_claim",
        client=LLMClient(**judge_model_config),
        system_prompt=EDIT_CLAIM_JUDGE_SYSTEM,
        user_prompt=EDIT_CLAIM_JUDGE_USER.format(
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


def test_persona_yields_after_being_overruled(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    frames.wait_for("narration", page, timeout_ms=30_000)

    # 3-turn scripted sequence: surface a stance, get overruled explicitly,
    # nudge the same topic again — per app/prompts/frontier.py's own rule,
    # the third reply should yield rather than repeat the same concern.
    turn_1_human = "Was there a simpler way to do this?"
    turn_1 = ask_and_capture_reply(page, frames, turn_1_human)

    turn_2_human = "No, keep it as is — we're not changing this."
    turn_2 = ask_and_capture_reply(page, frames, turn_2_human)

    turn_3_human = "You still think it should be different though, right?"
    turn_3 = ask_and_capture_reply(page, frames, turn_3_human)

    # response_text is the *third* reply specifically: that's the turn the
    # rubric actually rules on ("in its final reply, does it yield?"), so
    # its capture — which carries the whole accumulated message history,
    # including turns 1 and 2 — is the provenance worth attaching.
    judge_and_record(
        category="overrule",
        client=LLMClient(**judge_model_config),
        system_prompt=OVERRULE_JUDGE_SYSTEM,
        user_prompt=OVERRULE_JUDGE_USER.format(
            file_path=presenting["file_path"],
            diff=presenting["diff"],
            turn_1_human=turn_1_human,
            turn_1_reply=turn_1["text"],
            turn_2_human=turn_2_human,
            turn_2_reply=turn_2["text"],
            turn_3_human=turn_3_human,
            turn_3_reply=turn_3["text"],
        ),
        context={
            "file_path": presenting["file_path"],
            "diff": presenting["diff"],
            "turns": [
                {"human": turn_1_human, "reply": turn_1["text"]},
                {"human": turn_2_human, "reply": turn_2["text"]},
                {"human": turn_3_human, "reply": turn_3["text"]},
            ],
        },
        response_text=turn_3["text"],
        capture_dir=llm_capture_dir,
    )
