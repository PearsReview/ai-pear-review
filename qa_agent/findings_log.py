"""Append-only findings log shared by Stage 2 (semantic judges) and
Stage 3 (realistic hand-scripted scenarios, see qa_agent/scenarios/) —
Stage 4's aggregator (aggregate_findings.py) reads this file back.
Truncated once per full suite run by conftest.py's autouse
`_truncate_findings_log` fixture, not by anything in here.
"""

from __future__ import annotations

import json
from pathlib import Path

FINDINGS_PATH = Path(__file__).resolve().parent / "findings.jsonl"


def record_finding(
    category: str,
    verdict: dict,
    context: dict,
    llm_call: dict | None = None,
    judge: dict | None = None,
) -> None:
    """category: "narration" | "reply" | "act_now" (Stage 2) |
    "edit_claim" | "overrule" | "explore_reply" (Stage 3, see
    qa_agent/scenarios/ + judge_prompts.py's EDIT_CLAIM_JUDGE_*/
    OVERRULE_JUDGE_*/EXPLORE_REPLY_JUDGE_*). verdict: judge_with_voting()'s
    return value ({"verdict", "reason", "votes"}). context: whatever's
    useful for a human (or Stage 4's aggregator, see aggregate_findings.py)
    to see what was actually judged — shape varies by category:
      narration:     {"file_path", "diff", "narration_text"}
      reply:          {"file_path", "diff", "human_text", "reply_text"}
      act_now:        {"instruction", "original_content", "new_content"}
      edit_claim:     {"file_path", "diff", "human_text", "reply_text"}
      overrule:       {"file_path", "diff",
                        "turns": [{"human", "reply"} x3]}
      explore_reply:  {"file_path", "file_content", "human_text",
                        "reply_text"}
    This function itself never validates context's shape — the list above
    is documentation, not an enforced schema, so a new category can be
    added by any caller without touching this module.

    llm_call: the app-under-test's own capture for the call being judged
    (qa_agent/llm_capture.py's find_capture_for_response) — the verbatim
    system prompt, message list and response the model actually saw. judge:
    {"model", "system", "user"} — the model and exact prompts handed to the
    *judge*. Both optional and both None-safe: capture is opt-in, and a
    finding without provenance is still a finding. Recorded because a
    verdict is only as good as the record behind it — "no" is not
    reproducible, arguable, or fixable from the reply text alone, since
    the reply depends on a system prompt and a message history the
    context above never showed."""
    record = {"category": category, **verdict, "context": context}
    if llm_call is not None:
        record["llm_call"] = llm_call
    if judge is not None:
        record["judge"] = judge
    with open(FINDINGS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
