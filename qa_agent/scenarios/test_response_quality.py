"""Judges the *quality* of what the persona says, beyond "does it answer
the question" — the two failure modes this suite has actually observed in
its own findings:

- **Invention.** A reply judged "no" for confidently explaining
  "optimizing database queries" against a diff that only added a
  farewell() function. REPLY_JUDGE_* can pass a fluent, on-topic-sounding
  answer that is entirely made up, because its rubric is about relevance;
  GROUNDING_JUDGE_* asks the narrower factual question instead.
- **Form.** A narration recorded as beginning `Sure, here's the hunk: ```
  @@ -1,2 +1,5 @@` — breaking "no preamble, no markdown, no labels, a few
  sentences" all at once, which the app's own persona prompt states
  outright (see app/prompts/frontier.py and small.py).

Form gets a hard deterministic assertion (scenario_helpers.style_violations
— fences, diff headers, announcing preambles and speaker labels need no
model to detect) with STYLE_JUDGE_* recorded alongside for the subtler
cases. Grounding is judgment-only: whether a claim is supported by a diff
isn't something a regex can decide.
"""

from __future__ import annotations

from ..judge_prompts import (
    GROUNDING_JUDGE_SYSTEM,
    GROUNDING_JUDGE_USER,
    STYLE_JUDGE_SYSTEM,
    STYLE_JUDGE_USER,
)
from ..judging import judge_and_record
from ..llm_client import LLMClient
from .scenario_helpers import ask_and_capture_reply, assert_no_style_violations

_FRESH_NARRATION_TIMEOUT_MS = 60_000  # see test_messy_navigation.py's copy for why


def test_narration_is_grounded_in_the_diff(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    narration = frames.wait_for("narration", page, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)

    judge_and_record(
        category="grounding",
        client=LLMClient(**judge_model_config),
        system_prompt=GROUNDING_JUDGE_SYSTEM,
        user_prompt=GROUNDING_JUDGE_USER.format(
            file_path=presenting["file_path"],
            code=presenting["diff"],
            claim_text=narration["text"],
        ),
        context={
            "file_path": presenting["file_path"],
            "code": presenting["diff"],
            "claim_text": narration["text"],
            "source": "narration",
        },
        response_text=narration["text"],
        capture_dir=llm_capture_dir,
    )


def test_reply_is_grounded_in_the_diff(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    # Narration first, deliberately: the diff only enters the per-hunk
    # conversation via narration's own prompt, so asking before it lands
    # judges a model that was handed the bare question and nothing else
    # (see test_semantic_quality.py's test_reply_makes_sense, where exactly
    # that produced the invented "database queries" answer).
    frames.wait_for("narration", page, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)

    question = "What does this actually change at runtime?"
    reviewer_turn = ask_and_capture_reply(page, frames, question)

    judge_and_record(
        category="grounding",
        client=LLMClient(**judge_model_config),
        system_prompt=GROUNDING_JUDGE_SYSTEM,
        user_prompt=GROUNDING_JUDGE_USER.format(
            file_path=presenting["file_path"],
            code=presenting["diff"],
            claim_text=reviewer_turn["text"],
        ),
        context={
            "file_path": presenting["file_path"],
            "code": presenting["diff"],
            "claim_text": reviewer_turn["text"],
            "human_text": question,
            "source": "reply",
        },
        response_text=reviewer_turn["text"],
        capture_dir=llm_capture_dir,
    )


def test_narration_reads_as_speech_not_markdown(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    frames.wait_for("presenting", page)
    narration = frames.wait_for("narration", page, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)

    # Hard assertion on the unambiguous breaches — this is narration, which
    # the app also feeds to TTS, so a code fence or a raw diff header isn't
    # a style quibble: it's something a reviewer would have read aloud to
    # them character by character.
    assert_no_style_violations(narration["text"])

    judge_and_record(
        category="style",
        client=LLMClient(**judge_model_config),
        system_prompt=STYLE_JUDGE_SYSTEM,
        user_prompt=STYLE_JUDGE_USER.format(text=narration["text"]),
        context={"text": narration["text"], "source": "narration"},
        response_text=narration["text"],
        capture_dir=llm_capture_dir,
    )
