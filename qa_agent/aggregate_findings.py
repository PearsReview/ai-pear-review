"""Stage 4 — reads qa_agent/findings.jsonl (Stage 2/3's judge verdicts) and,
if present, qa_agent/pytest_summary.json (Stage 1's mechanical pass/fail
counts, written by conftest.py's pytest_sessionfinish hook), and writes
qa_agent/results/results.md: one categorized, prioritized summary of what
was actually found — weak points before strong ones, with near-duplicate
failure reasons collapsed.

Run as a separate step after the suite, never during it — aggregation and
truncation (conftest.py's `_truncate_findings_log`) are deliberately two
different responsibilities on two different schedules, not one fixture:
truncating mid-session would race the last few tests' still-in-flight
record_finding calls if it ran as a session-scoped fixture teardown.

    pytest qa_agent/
    python -m qa_agent.aggregate_findings

Standalone: no server, no browser, no LLM call — just reads up to two
files and writes one. Written into qa_agent/results/ (not a bare
qa_agent/results.md) so it lands in the directory .gitignore already
ignores, rather than needing its own new ignore rule.
"""

from __future__ import annotations

import difflib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .findings_log import FINDINGS_PATH

RESULTS_DIR = FINDINGS_PATH.parent / "results"
RESULTS_PATH = RESULTS_DIR / "results.md"
PYTEST_SUMMARY_PATH = FINDINGS_PATH.parent / "pytest_summary.json"

_COLLAPSE_RATIO_THRESHOLD = 0.6
_VERDICT_ORDER = {"no": 0, "unsure": 1, "yes": 2}


def load_findings(path: Path = FINDINGS_PATH) -> list[dict]:
    """One json.loads per line. A line that fails to parse (e.g. a
    truncated last line from a killed process) is skipped, not fatal —
    reported as 0 findings from that line rather than crashing the whole
    report over one bad row. Missing file -> no findings, not an error
    (a completely fresh checkout that's never run the suite yet)."""
    if not path.exists():
        return []
    findings = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return findings


def load_pytest_summary(path: Path = PYTEST_SUMMARY_PATH) -> dict | None:
    """None if the file doesn't exist or fails to parse — render_markdown
    treats that as "no pytest data available" and still renders a
    findings-only report rather than erroring."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def group_and_prioritize(findings: list[dict]) -> dict[str, list[dict]]:
    """category -> its own findings, each category's list sorted "no"
    verdicts first, then "unsure", then "yes" — grouping by category is
    the actual prioritization (a human sees "the overrule check is weak"
    as one heading, not buried in one flat list); the sort within a group
    just puts the worst entries at the top of it."""
    by_category: dict[str, list[dict]] = defaultdict(list)
    for finding in findings:
        by_category[finding.get("category", "unknown")].append(finding)
    for items in by_category.values():
        items.sort(key=lambda f: _VERDICT_ORDER.get(f.get("verdict"), 3))
    return dict(by_category)


def collapse_repeated_reasons(findings: list[dict], threshold: float = _COLLAPSE_RATIO_THRESHOLD) -> list[dict]:
    """Collapses near-duplicate `reason` strings (within the same verdict,
    so a "no" is never folded into an "unsure" just because the wording is
    similar) into one entry carrying a `_seen` count — the same failure
    described in slightly different words across several runs of the same
    scenario reads as one bullet with a count, not N near-identical ones.
    difflib.SequenceMatcher is stdlib and already used elsewhere in this
    suite (test_semantic_quality.py's _assert_only_target_region_changed),
    so this adds no new dependency."""
    collapsed: list[dict] = []
    for finding in findings:
        reason = finding.get("reason", "")
        verdict = finding.get("verdict")
        match = next(
            (
                existing
                for existing in collapsed
                if existing.get("verdict") == verdict
                and difflib.SequenceMatcher(a=existing.get("reason", ""), b=reason).ratio() > threshold
            ),
            None,
        )
        if match is not None:
            match["_seen"] = match.get("_seen", 1) + 1
        else:
            collapsed.append({**finding, "_seen": 1})
    return collapsed


def _category_severity(items: list[dict]) -> tuple[int, int]:
    """Sort key for ordering categories worst-first: more "no" verdicts
    outranks more "unsure" ones — "unsure" is still worth a human's
    attention (a majority-vote judge collapsing to "unsure" often means
    the underlying behavior really is borderline), but sheer "unsure"
    volume should never outrank a category with a real "no" in it."""
    no_count = sum(1 for f in items if f.get("verdict") == "no")
    unsure_count = sum(1 for f in items if f.get("verdict") == "unsure")
    return (-no_count, -unsure_count)


def _fence(text: str, limit: int = 2000) -> list[str]:
    """A fenced block, truncated — a captured prompt can carry a whole file,
    and results.md is meant to be read, not scrolled past. The raw,
    untruncated record is always in findings.jsonl for anyone who needs it."""
    body = text if len(text) <= limit else text[:limit] + f"\n... [truncated, {len(text)} chars total]"
    return ["```", body, "```"]


def _provenance_block(finding: dict) -> list[str]:
    """A collapsed <details> with what the model was actually given, for a
    finding that has it (see findings_log.record_finding's llm_call/judge).

    This is the difference between "the reply hallucinated" and a diagnosis:
    the first real use of this showed a reply judged "no" whose captured
    messages were just [{"role": "user", "content": "Why was this change
    made?"}] — no diff, no history — which explained the hallucination
    outright rather than leaving it as a mystery about model quality."""
    llm_call = finding.get("llm_call")
    judge = finding.get("judge")
    if not llm_call and not judge:
        return []
    lines = ["", "  <details><summary>what the model was given</summary>", ""]
    if llm_call:
        lines.append(
            f"  App call: {llm_call.get('provider')}/{llm_call.get('model')} "
            f"(connection {llm_call.get('connection')}, call {llm_call.get('seq')}, "
            f"{llm_call.get('input_tokens')} in / {llm_call.get('output_tokens')} out)"
        )
        lines.append("")
        lines.append("  System prompt:")
        lines.extend(_fence(llm_call.get("system_prompt", "")))
        lines.append("")
        lines.append("  Messages:")
        for message in llm_call.get("messages", []):
            lines.append(f"  - **{message.get('role')}**:")
            lines.extend(_fence(message.get("content", ""), limit=1200))
        lines.append("")
        lines.append("  Response:")
        lines.extend(_fence(llm_call.get("response", ""), limit=1200))
    if judge:
        lines.append("")
        lines.append(f"  Judged by: {judge.get('model')}")
        lines.append("")
        lines.append("  Judge prompt:")
        lines.extend(_fence(judge.get("user", ""), limit=1200))
    lines.append("")
    lines.append("  </details>")
    return lines


def render_markdown(findings: list[dict], pytest_summary: dict | None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"# QA Findings — {timestamp}", "", "## Summary"]

    if pytest_summary is not None:
        passed = len(pytest_summary.get("passed", []))
        failed = pytest_summary.get("failed", [])
        errored = pytest_summary.get("error", [])
        skipped = pytest_summary.get("skipped", [])
        total = passed + len(failed) + len(errored) + len(skipped)
        summary_line = f"- Stage 1 (mechanical): {passed}/{total} passed"
        broken = [r["nodeid"] for r in [*failed, *errored]]
        if broken:
            summary_line += f", {len(broken)} failed/errored: {', '.join(broken)}"
        lines.append(summary_line)
    else:
        lines.append(
            "- Stage 1 (mechanical): no pytest_summary.json found — run `pytest qa_agent/` first for this line"
        )

    verdict_counts: dict[str, int] = defaultdict(int)
    for finding in findings:
        verdict_counts[finding.get("verdict", "unknown")] += 1
    lines.append(
        f"- Stage 2/3 (LLM-judged): {len(findings)} checks recorded — "
        f"{verdict_counts.get('yes', 0)} yes, {verdict_counts.get('no', 0)} no, "
        f"{verdict_counts.get('unsure', 0)} unsure"
    )
    lines.append("")

    by_category = group_and_prioritize(findings)

    weak_categories = sorted(
        (
            (category, items)
            for category, items in by_category.items()
            if any(f.get("verdict") in ("no", "unsure") for f in items)
        ),
        key=lambda pair: _category_severity(pair[1]),
    )

    lines.append("## Weak points (sorted worst first)")
    if not weak_categories:
        lines.append("")
        lines.append('None — every recorded finding this run was "yes".')
    for category, items in weak_categories:
        no_count = sum(1 for f in items if f.get("verdict") == "no")
        unsure_count = sum(1 for f in items if f.get("verdict") == "unsure")
        lines.append("")
        lines.append(f'### {category} — {no_count} "no", {unsure_count} "unsure" (of {len(items)} recorded)')
        weak_items = [f for f in items if f.get("verdict") in ("no", "unsure")]
        for finding in collapse_repeated_reasons(weak_items):
            seen_note = f" (seen {finding['_seen']}x)" if finding.get("_seen", 1) > 1 else ""
            lines.append(
                f"- **{finding.get('verdict')}**{seen_note}: {finding.get('reason') or '(no reason recorded)'}"
            )
            lines.extend(_provenance_block(finding))
    lines.append("")

    strong_categories = sorted(
        category for category, items in by_category.items() if items and all(f.get("verdict") == "yes" for f in items)
    )
    lines.append("## Strong points")
    if not strong_categories:
        lines.append("")
        lines.append("None yet — every category above has at least one weak finding.")
    for category in strong_categories:
        count = len(by_category[category])
        lines.append(f'- {category}: {count}/{count} "yes"')
    lines.append("")

    lines.append("## All recorded findings (raw)")
    lines.append("")
    if not findings:
        lines.append("No findings recorded this run.")
    for finding in findings:
        lines.append(f"- {finding.get('category')} | {finding.get('verdict')} | {finding.get('reason', '')}")

    return "\n".join(lines) + "\n"


def main() -> None:
    findings = load_findings()
    pytest_summary = load_pytest_summary()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(render_markdown(findings, pytest_summary), encoding="utf-8")
    print(f"wrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()
