"""Drives the app against a real repo's real uncommitted changes end to
end, and writes what it saw to a pack a human (or a fresh Claude Code
session) can read.

Unlike the sibling suites, there is no oracle here — no RepoSpec, no known-
good symbol list — because the whole point is testing against a repo that
wasn't built to be easy to test against. So the assertions in this file are
deliberately mechanical (did every hunk narrate, did the server ever send
an "error" frame, did the walk actually reach the end, did every hunk get a
recorded judge verdict) rather than assertions ON the judge's verdicts
themselves — same "record, don't hard-assert" convention
test_semantic_quality.py documents at length (a small local model judging
its own kind of output is not reliable enough to gate a pass/fail on, see
that file's module docstring and README.md's Stage 2 section). The written
pack — now including each hunk's verdict/reason alongside its diff and
narration — is where an actual quality read happens, by a human or a fresh
Claude Code session, same as the generated suite's narration pack.

Run with:

    pytest qa_agent/live/ --target-repo path/to/repo
"""

from __future__ import annotations

import collections
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qa_agent.judge_prompts import (
    NARRATION_JUDGE_SYSTEM,
    NARRATION_JUDGE_USER,
    REPLY_JUDGE_SYSTEM,
    REPLY_JUDGE_USER,
)
from qa_agent.judging import judge_and_record
from qa_agent.llm_client import LLMClient
from qa_agent.ws_capture import WsFrames

from .conftest import _suppress_auto_tour

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# k=1, NOT test_semantic_quality.py's k=3 default — deliberately different
# from the sibling suite, and worth explaining why. k=3 self-consistency
# voting exists to make a STANDALONE local verdict trustworthy on its own.
# This suite's local verdict isn't standalone: judge-live-review
# (.claude/skills/judge-live-review/) always re-checks every item
# afterward with an actual capable model, and specifically surfaces where
# it disagrees with this one — that disagreement signal is the point, not
# a fallback for when this judge is unreliable alone. So the local judge's
# job here has shrunk to "cheap, free, automatic baseline to diff
# against," which one vote serves as well as three, at a third of the
# Ollama calls. Also fixes a real problem: k=3 across both narration and
# reply roughly doubled this suite's per-hunk Ollama round-trips, and a
# real run timed out waiting on a narration stuck behind that backlog —
# see git history.
JUDGE_VOTES = 1

# Same question test_reply_makes_sense (qa_agent/test_semantic_quality.py)
# asks — a deliberately generic prompt, so the judge is checking "is this
# answer consistent with the diff", not "did it answer this exact phrasing
# well". Asked once per hunk, after that hunk's narration has landed (the
# per-hunk conversation history is empty until then — see that sibling
# test's own comment on the race this avoids).
REPLY_QUESTION = "Why was this change made?"

# A hunk's first narration is two chained model calls (briefing, then
# narration) — measured at up to ~40s/hunk on a CPU-only Ollama setup per
# this project's own README, plus a possible one-off model load if Ollama
# had unloaded it. Generous on purpose: see qa_agent/generated/conftest.py's
# identical reasoning for FRESH_NARRATION_TIMEOUT_MS.
#
# Both bumped from 120s/60s after a real run timed out waiting for a
# narration that had 2 judge calls' worth of extra sequential Ollama
# traffic (reply + reply's own k=3 judge votes) queued immediately ahead
# of it on the same hunk — this suite now does roughly double the Ollama
# round-trips per hunk it did when those numbers were first measured.
NARRATION_TIMEOUT_MS = 180_000
REPLY_TIMEOUT_MS = 90_000


def _wait_for_frame_number(frames: WsFrames, page, msg_type: str, n: int, timeout_ms: int) -> dict:
    """Waits for the nth frame of msg_type across the whole session, not
    just the newest existing one — WsFrames.wait_for returns the newest
    match immediately, which on hunk 2 would hand back hunk 1's leftover
    narration/reply. Shared by narration and reviewer_turn waits below;
    same staleness trap qa_agent/generated/conftest.py's identically-named
    helper exists to avoid, just generalized to one msg_type parameter."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        matches = [f for f in frames.frames if f.get("type") == msg_type]
        if len(matches) >= n:
            return matches[n - 1]["payload"]
        page.wait_for_timeout(500)
    raise TimeoutError(
        f"{msg_type} #{n} never arrived within {timeout_ms}ms "
        f"(saw {len([f for f in frames.frames if f.get('type') == msg_type])})"
    )


@pytest.fixture(scope="session")
def narrated_walk(app_server: str, playwright, target_repo: Path, total_hunks: int, judge_model_config: dict) -> dict:
    """Starts the review and clicks Next exactly total_hunks - 1 times.
    For each hunk: waits for narration, judges it (category "narration"),
    then asks REPLY_QUESTION and judges the reply too (category "reply",
    REPLY_JUDGE_* — diff-consistency, not the narration rubric). Also
    collects any `error` frame seen anywhere in the session. Both
    categories record to the shared qa_agent/findings.jsonl, same schema
    Stage 2's test_semantic_quality.py writes — Stage 4's
    aggregate_findings.py reads them back same as always. Session-scoped:
    this is by far the most expensive fixture in the suite (a briefing +
    narration + reply + 2*k judge calls per hunk against a real local
    model), and every test below reads the same walk rather than
    repeating it.

    Bounded by total_hunks rather than #next-btn's enabled state — see
    that fixture's docstring for why the button can't be used as a
    termination signal here.

    Not truncating findings.jsonl before appending — same choice
    qa_agent/generated/ already makes (only the fixed suite's conftest
    truncates it); see findings_log.py's own module docstring."""
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    frames = WsFrames()
    frames.attach(page)
    judge = LLMClient(**judge_model_config)
    try:
        page.goto(app_server, wait_until="load")
        page.wait_for_selector("#status-bar span", timeout=15000)
        page.click("#start-review-btn", timeout=10000)

        entries: list[dict] = []
        for i in range(total_hunks):
            narration = _wait_for_frame_number(frames, page, "narration", len(entries) + 1, NARRATION_TIMEOUT_MS)
            presenting = frames.wait_for("presenting", page, timeout_ms=30_000)
            file_path = presenting.get("file_path", "")
            diff = presenting.get("diff", "")
            narration_text = narration.get("text", "")

            narration_verdict = judge_and_record(
                category="narration",
                client=judge,
                system_prompt=NARRATION_JUDGE_SYSTEM,
                user_prompt=NARRATION_JUDGE_USER.format(file_path=file_path, diff=diff, narration_text=narration_text),
                context={"file_path": file_path, "diff": diff, "narration_text": narration_text},
                k=JUDGE_VOTES,
            )

            page.fill("#text-input", REPLY_QUESTION)
            page.click("#send-btn")
            reply = _wait_for_frame_number(frames, page, "reviewer_turn", i + 1, REPLY_TIMEOUT_MS)
            reply_text = reply.get("text", "")

            reply_verdict = judge_and_record(
                category="reply",
                client=judge,
                system_prompt=REPLY_JUDGE_SYSTEM,
                user_prompt=REPLY_JUDGE_USER.format(
                    file_path=file_path, diff=diff, human_text=REPLY_QUESTION, reply_text=reply_text
                ),
                context={"file_path": file_path, "diff": diff, "human_text": REPLY_QUESTION, "reply_text": reply_text},
                k=JUDGE_VOTES,
            )

            entries.append(
                {
                    "file_path": file_path,
                    "diff": diff,
                    "narration_text": narration_text,
                    "verdict": narration_verdict,
                    "reply_text": reply_text,
                    "reply_verdict": reply_verdict,
                }
            )
            if i < total_hunks - 1:
                page.locator("#next-btn").click()
                page.wait_for_timeout(500)

        errors = [f["payload"] for f in frames.frames if f.get("type") == "error"]
        return {"entries": entries, "errors": errors, "repo": target_repo}
    finally:
        browser.close()


def _format_verdict(verdict: dict | None) -> str:
    verdict = verdict or {}
    votes = verdict.get("votes") or []
    tally = ", ".join(
        f"{v}x{sum(1 for vote in votes if vote['verdict'] == v)}"
        for v in dict.fromkeys(vote["verdict"] for vote in votes)
    )
    return f"{verdict.get('verdict', 'n/a')} ({tally}) — {verdict.get('reason', '')}"


def _write_pack(walk: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    repo_name = walk["repo"].name
    path = RESULTS_DIR / f"{repo_name}_live_review_pack.md"
    lines = [f"# Live review pack — {repo_name}", ""]
    lines.append(f"{len(walk['entries'])} hunk(s) walked, {len(walk['errors'])} error frame(s) seen.")
    lines.append("")
    for i, entry in enumerate(walk["entries"], start=1):
        lines.append(f"## Hunk {i} — {entry['file_path']}")
        lines.append("")
        lines.append("```diff")
        lines.append(entry["diff"].rstrip("\n"))
        lines.append("```")
        lines.append("")
        lines.append("**Narration:**")
        lines.append("")
        lines.append(entry["narration_text"] or "*(empty)*")
        lines.append("")
        lines.append(f"**Judge verdict (narration):** {_format_verdict(entry.get('verdict'))}")
        lines.append("")
        lines.append(f"**Q:** {REPLY_QUESTION}")
        lines.append("")
        lines.append(f"**A:** {entry.get('reply_text') or '*(empty)*'}")
        lines.append("")
        lines.append(f"**Judge verdict (reply):** {_format_verdict(entry.get('reply_verdict'))}")
        lines.append("")
    if walk["errors"]:
        lines.append("## Error frames seen during the walk")
        lines.append("")
        for err in walk["errors"]:
            lines.append(f"- {err}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _item_content_hash(item: dict) -> str:
    """Identifies one judged item by what was actually judged, not its
    position — so re-running the walk after target_repo's diff shifts
    (a hunk added/removed upstream of this one) doesn't make the
    judge-live-review skill re-judge everything just because indices moved.
    Mirrored (not imported) in .claude/skills/judge-live-review/scan_pack.py
    — skills stay standalone from qa_agent the same way
    prep-review/scan_hunks.py's own copy of the app's hashing rules does;
    see that file's module docstring. Keep both copies in sync by hand."""
    material = "\x00".join(
        [
            item.get("category", ""),
            item.get("file_path", ""),
            item.get("diff") or "",
            item.get("question") or "",
            item.get("answer_text", ""),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _write_pack_json(walk: dict) -> Path:
    """A machine-readable sibling to the .md pack — same underlying data,
    reshaped so a script (rather than a markdown scraper) can drive
    judgment over it. Two items per hunk (narration, reply) rather than
    one — category/question/answer_text is generalized specifically so
    that split needs no reshape: "question" stays null for narration
    (which has none) and carries REPLY_QUESTION for the reply item.

    Consumed by .claude/skills/judge-live-review/ — a human-run skill, not
    anything this suite calls itself. This file writes it; it never reads
    it back or judges anything with it."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    repo_name = walk["repo"].name
    path = RESULTS_DIR / f"{repo_name}_live_review_pack.json"
    items = []
    for i, entry in enumerate(walk["entries"]):
        narration_item = {
            "index": i,
            "category": "narration",
            "file_path": entry["file_path"],
            "diff": entry["diff"],
            "question": None,
            "answer_text": entry["narration_text"],
            "local_verdict": entry.get("verdict"),
        }
        narration_item["content_hash"] = _item_content_hash(narration_item)
        items.append(narration_item)

        reply_item = {
            "index": i,
            "category": "reply",
            "file_path": entry["file_path"],
            "diff": entry["diff"],
            "question": REPLY_QUESTION,
            "answer_text": entry.get("reply_text", ""),
            "local_verdict": entry.get("reply_verdict"),
        }
        reply_item["content_hash"] = _item_content_hash(reply_item)
        items.append(reply_item)
    data = {
        "repo": repo_name,
        "target_repo": str(walk["repo"]),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "error_count": len(walk["errors"]),
        "items": items,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def test_diff_loaded_at_least_one_file(page):
    """The most basic thing that has to be true before anything else here
    means anything: the app actually found target_repo's changes."""
    items = page.locator("#file-list-items > *")
    assert items.count() > 0, "file explorer is empty — did the app find target_repo's diff at all?"


def test_every_hunk_was_narrated(narrated_walk):
    entries = narrated_walk["entries"]
    assert entries, "no hunks were walked at all"
    empty = [e["file_path"] for e in entries if not e["narration_text"].strip()]
    assert not empty, f"empty narration for: {empty}"


def test_every_hunk_got_a_reply(narrated_walk):
    """Companion to test_every_hunk_was_narrated, for the reply asked
    about each hunk (REPLY_QUESTION) rather than its narration — the two
    are separate WS exchanges (narration is unprompted; the reply answers
    an explicit question) and a stuck/empty reply wouldn't be caught by
    the narration check alone."""
    entries = narrated_walk["entries"]
    empty = [e["file_path"] for e in entries if not (e.get("reply_text") or "").strip()]
    assert not empty, f"empty reply to {REPLY_QUESTION!r} for: {empty}"


def test_no_error_frames_during_the_walk(narrated_walk):
    assert not narrated_walk["errors"], f"server sent error frame(s): {narrated_walk['errors']}"


def _assert_verdicts_recorded(entries: list[dict], verdict_key: str, label: str) -> None:
    missing = [e["file_path"] for e in entries if not (e.get(verdict_key) or {}).get("verdict")]
    assert not missing, f"no {label} judge verdict recorded for: {missing}"
    tally = collections.Counter(e[verdict_key]["verdict"] for e in entries)
    print(f"\n{label} judge tally across {len(entries)} hunk(s): {dict(tally)}")


def test_judge_recorded_a_verdict_for_every_hunk(narrated_walk):
    """Structural, not a quality gate: every hunk has to have actually been
    judged (a verdict of "yes"/"no"/"unsure" — see llm_client.LLMClient.judge,
    which never raises and falls back to "unsure" on any failure) before the
    *content* of any verdict means anything. Whether narration is actually
    good is what the written pack is for a human to read — see this
    module's own docstring for why nothing here hard-asserts on verdict
    values themselves."""
    _assert_verdicts_recorded(narrated_walk["entries"], "verdict", "narration")


def test_judge_recorded_a_verdict_for_every_reply(narrated_walk):
    """Same structural check as test_judge_recorded_a_verdict_for_every_hunk,
    for the reply category (REPLY_JUDGE_* — diff-consistency, not the
    narration rubric)."""
    _assert_verdicts_recorded(narrated_walk["entries"], "reply_verdict", "reply")


def test_review_pack_is_written(narrated_walk):
    md_path = _write_pack(narrated_walk)
    json_path = _write_pack_json(narrated_walk)
    print(f"\nlive review pack written to {md_path}")
    print(f"machine-readable pack written to {json_path} — run judge-live-review against it")
    for path in (md_path, json_path):
        assert path.exists()
        assert path.stat().st_size > 0
