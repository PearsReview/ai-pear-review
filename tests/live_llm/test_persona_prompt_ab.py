"""A/B tests app/prompts/small_candidate.py's revised PERSONA_SYSTEM
against the real, currently-shipping app/prompts/small.py PERSONA_SYSTEM
— same real hunks, same model, same judge, only the persona prompt
differs. See small_candidate.py's own module docstring for exactly which
three confirmed failure modes this candidate targets and why.

This is a comparison, not a verdict: the sample here (one target
repo's hunks) is small, and two of the three failure modes are
already known to be probabilistic (recorded once, absent on a later run
of the identical hunk) — a single run "winning" doesn't prove the
candidate is better, only that it's worth more runs. What IS asserted
here is deterministic and doesn't need a larger sample: the candidate
must not introduce style violations the current prompt didn't have.

Deliberately does NOT touch app/prompts/small.py, select_prompts(), or
any other production wiring — the candidate lives entirely in its own
module and is only ever imported here.

Run with:

    pytest tests/live_llm/test_persona_prompt_ab.py -v -s
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qa_agent.judge_prompts import NARRATION_JUDGE_SYSTEM, NARRATION_JUDGE_USER
from qa_agent.judging import judge_and_record
from qa_agent.llm_client import LLMClient
from qa_agent.scenarios.scenario_helpers import style_violations

from .conftest import RESULTS_DIR, project_context_for

JUDGE_VOTES = 1  # see qa_agent/live/test_live_review.py's JUDGE_VOTES for the reasoning

# Not a hardened production check (see module docstring) — a narrow net
# for the exact failure recorded live: attributing a claim to a source
# that was never part of the prompt. Narration never legitimately has a
# real "review"/"team"/"ticket" to refer to, so these are safe to flag
# unconditionally in this context.
_FABRICATED_SOURCE_PATTERNS = [
    re.compile(r"\bthe review\b", re.IGNORECASE),
    re.compile(r"\bthe team\b", re.IGNORECASE),
    re.compile(r"\bthe ticket\b", re.IGNORECASE),
    re.compile(r"\bthe pr\b", re.IGNORECASE),
    re.compile(r"\bthey (?:mentioned|said|decided|told|wanted)\b", re.IGNORECASE),
]


def _fabricated_source_hits(text: str) -> list[str]:
    return [p.pattern for p in _FABRICATED_SOURCE_PATTERNS if p.search(text)]


def _narrate(conversation, briefing_client, target_repo, hunks, hunk, prompt_set):
    """One narration call under a specific PromptSet — swaps `.prompts`
    on the shared client immediately before the call and restores it
    after, so this can't leak into any other test sharing `conversation`."""
    original = conversation.prompts
    conversation.prompts = prompt_set
    try:
        briefing = briefing_client.analyze_hunk(hunk, conversation)
        context = project_context_for(target_repo, hunks, hunk)
        _, narration_text = conversation.present_hunk([], hunk, briefing, context)
        return narration_text
    finally:
        conversation.prompts = original


@pytest.fixture(scope="session")
def ab_walk(hunks, conversation, briefing_client, target_repo, judge_model_config) -> dict:
    from app.prompts import PromptSet, small_candidate
    from app.prompts import small as current_small

    current_prompts = PromptSet(
        persona_system=current_small.PERSONA_SYSTEM,
        briefing_system=current_small.BRIEFING_SYSTEM,
    )
    candidate_prompts = PromptSet(
        persona_system=small_candidate.PERSONA_SYSTEM,
        briefing_system=current_small.BRIEFING_SYSTEM,  # only persona_system is under test
    )

    judge = LLMClient(**judge_model_config)
    results = {"current": [], "candidate": []}

    for hunk in hunks:
        for variant_name, prompt_set in (("current", current_prompts), ("candidate", candidate_prompts)):
            text = _narrate(conversation, briefing_client, target_repo, hunks, hunk, prompt_set)
            verdict = judge_and_record(
                category="narration",
                client=judge,
                system_prompt=NARRATION_JUDGE_SYSTEM,
                user_prompt=NARRATION_JUDGE_USER.format(
                    file_path=hunk.file_path, diff=hunk.diff_context, narration_text=text
                ),
                context={
                    "file_path": hunk.file_path,
                    "diff": hunk.diff_context,
                    "narration_text": text,
                    "prompt_variant": variant_name,
                },
                k=JUDGE_VOTES,
            )
            results[variant_name].append(
                {
                    "file_path": hunk.file_path,
                    "diff": hunk.diff_context,
                    "narration_text": text,
                    "style_violations": style_violations(text),
                    "fabricated_source_hits": _fabricated_source_hits(text),
                    "local_verdict": verdict,
                }
            )

    return results


def _write_report(results: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / "persona_prompt_ab.md"
    lines = ["# Persona prompt A/B — current vs candidate", ""]
    for variant in ("current", "candidate"):
        entries = results[variant]
        style_hits = sum(1 for e in entries if e["style_violations"])
        fab_hits = sum(1 for e in entries if e["fabricated_source_hits"])
        yes = sum(1 for e in entries if e["local_verdict"]["verdict"] == "yes")
        lines.append(f"## {variant} ({len(entries)} hunks)")
        lines.append("")
        lines.append(f"- style violations: {style_hits}/{len(entries)}")
        lines.append(f"- fabricated-source hits: {fab_hits}/{len(entries)}")
        lines.append(f"- local judge 'yes': {yes}/{len(entries)}")
        lines.append("")
        for e in entries:
            flags = e["style_violations"] + e["fabricated_source_hits"]
            flag_str = f" ⚠ {', '.join(flags)}" if flags else ""
            lines.append(f"### {e['file_path']}{flag_str}")
            lines.append("")
            lines.append(e["narration_text"])
            lines.append("")
            lines.append(f"local verdict: {e['local_verdict']['verdict']} — {e['local_verdict']['reason']}")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")

    json_path = path.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def test_both_variants_produced_narration(ab_walk):
    for variant, entries in ab_walk.items():
        empty = [e["file_path"] for e in entries if not e["narration_text"].strip()]
        assert not empty, f"{variant}: empty narration for {empty}"


def test_candidate_introduces_no_new_style_violations(ab_walk):
    """The one thing this A/B test can actually assert on with a sample
    this small: style_violations() is deterministic, so if the candidate
    prompt's own no-code-fence example somehow made things worse instead
    of better, that would show up as a real regression here, not noise."""
    current_violations = {e["file_path"] for e in ab_walk["current"] if e["style_violations"]}
    candidate_violations = {e["file_path"] for e in ab_walk["candidate"] if e["style_violations"]}
    new_violations = candidate_violations - current_violations
    assert not new_violations, f"candidate prompt introduced NEW style violations on: {new_violations}"


def test_report_is_written(ab_walk):
    path = _write_report(ab_walk)
    print(f"\npersona prompt A/B report written to {path}")

    for variant, entries in ab_walk.items():
        style_hits = sum(1 for e in entries if e["style_violations"])
        fab_hits = sum(1 for e in entries if e["fabricated_source_hits"])
        yes = sum(1 for e in entries if e["local_verdict"]["verdict"] == "yes")
        print(
            f"  {variant}: {style_hits} style violation(s), {fab_hits} fabricated-source hit(s), "
            f"{yes}/{len(entries)} local judge 'yes'"
        )

    assert path.exists() and path.stat().st_size > 0
