"""Prototype: the same kind of accuracy question qa_agent/live/ asks, via
direct in-process calls instead of Playwright — no browser, no WS, no
"walk N hunks to reach hunk K". Prints wall-clock timing for each call so
the actual speed difference is measured, not assumed.

Judges each answer with the local Ollama judge (k=1, same convention as
qa_agent/live/ — see that suite's JUDGE_VOTES for the reasoning) and
writes a pack compatible with judge-live-review, so the exact same skill
that judges qa_agent/live/'s results can judge these too with zero
changes.

Run with:

    pytest tests/live_llm/ -v -s --target-repo path/to/repo
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qa_agent.judge_prompts import (
    DEPENDENCY_JUDGE_SYSTEM,
    DEPENDENCY_JUDGE_USER,
    EXPLORE_REPLY_JUDGE_SYSTEM,
    EXPLORE_REPLY_JUDGE_USER,
    NARRATION_JUDGE_SYSTEM,
    NARRATION_JUDGE_USER,
    REPLY_JUDGE_SYSTEM,
    REPLY_JUDGE_USER,
)
from qa_agent.judging import judge_and_record
from qa_agent.llm_client import LLMClient

from .conftest import RESULTS_DIR, project_context_for

_ASK_DEPENDENCY = False

JUDGE_VOTES = 1  # see qa_agent/live/test_live_review.py's JUDGE_VOTES for the full reasoning


def _matching_symbol(call_map_data: dict, hunk) -> dict | None:
    """The first call-map symbol defined in this hunk's file whose name
    appears in this hunk's own diff text — the same match
    app/services/call_map.py's call_map_prompt_block makes to decide
    whether to inject a caller fact for this hunk at all. Picks the symbol
    with the most recorded callers among matches, same tie-break
    qa_agent/live/test_live_dependency_accuracy.py's dependency_target
    fixture uses, for the most informative single question per hunk."""
    normalized_file = hunk.file_path.replace("\\", "/")
    candidates = [
        symbol
        for symbol in call_map_data.get("symbols", [])
        if symbol["file"] == normalized_file and symbol["name"] in hunk.diff_context
    ]
    return max(candidates, key=lambda s: s["caller_count"], default=None)


def _item_content_hash(item: dict) -> str:
    """Identical formula to qa_agent/live/test_live_review.py's
    _item_content_hash — must match exactly for scan_pack.py to key items
    consistently regardless of which suite produced them."""
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


@pytest.fixture(scope="session")
def timing() -> dict:
    """Collects {label: seconds} for every LLM call this session makes, so
    the final test can print a summary — the actual evidence for "is this
    quicker", not a guess."""
    return {}


@pytest.fixture(scope="session")
def direct_walk(hunks, conversation, briefing_client, target_repo, call_map_data, judge_model_config, timing) -> dict:
    """Every hunk's narration+reply, a dependency question for every hunk
    that touches a symbol call_map_data records real callers for, several
    explore-mode questions across different real unchanged files, and one
    project-level question — each timed and judged. Session-scoped: every
    test below reads this same walk.

    Covers every hunk (not a sample) specifically because direct calls
    don't pay qa_agent/live/'s walk-to-hunk-K cost — there's no longer a
    reason to hold back once that constraint is gone."""
    judge = LLMClient(**judge_model_config)
    entries = []

    # Warm-up: ConversationClient.timeout (app/config.yaml's
    # conversation.timeout_seconds, 60s) is a hard ceiling ENFORCED INSIDE
    # _send/generate_once, not just a test-side wait — confirmed live,
    # this suite's own first real call once exceeded it and raised
    # ConversationError, most likely because Ollama had unloaded the model
    # during the idle gap between test runs (keep_alive default ~5min).
    # That's a genuine app-level finding, not a test bug: a real reviewer
    # whose first request lands right after an idle unload could hit the
    # exact same 60s ceiling on a cold multi-GB load. Not fixed here (that
    # would mean changing production config.yaml on this suite's say-so) —
    # worked around instead, by forcing the load to happen against a
    # throwaway call that isn't scored as one of this walk's real timings.
    try:
        conversation.generate_once("Reply with the single word OK.", system_prompt="You are a test.")
    except Exception:
        pass  # best-effort — a real failure here surfaces again on the first real call below anyway

    for hunk in hunks:
        t0 = time.time()
        briefing = briefing_client.analyze_hunk(hunk, conversation)
        context = project_context_for(target_repo, hunks, hunk)
        history, narration_text = conversation.present_hunk([], hunk, briefing, context)
        timing[f"narration[{hunk.file_path}]"] = time.time() - t0

        t0 = time.time()
        history, reply_text = conversation.respond_to_reviewer(history, "Why was this change made?")
        timing[f"reply[{hunk.file_path}]"] = time.time() - t0

        narration_verdict = judge_and_record(
            category="narration",
            client=judge,
            system_prompt=NARRATION_JUDGE_SYSTEM,
            user_prompt=NARRATION_JUDGE_USER.format(
                file_path=hunk.file_path, diff=hunk.diff_context, narration_text=narration_text
            ),
            context={"file_path": hunk.file_path, "diff": hunk.diff_context, "narration_text": narration_text},
            k=JUDGE_VOTES,
        )
        reply_verdict = judge_and_record(
            category="reply",
            client=judge,
            system_prompt=REPLY_JUDGE_SYSTEM,
            user_prompt=REPLY_JUDGE_USER.format(
                file_path=hunk.file_path,
                diff=hunk.diff_context,
                human_text="Why was this change made?",
                reply_text=reply_text,
            ),
            context={
                "file_path": hunk.file_path,
                "diff": hunk.diff_context,
                "human_text": "Why was this change made?",
                "reply_text": reply_text,
            },
            k=JUDGE_VOTES,
        )
        entries.append(
            {
                "category": "narration",
                "file_path": hunk.file_path,
                "diff": hunk.diff_context,
                "question": None,
                "answer_text": narration_text,
                "local_verdict": narration_verdict,
            }
        )
        entries.append(
            {
                "category": "reply",
                "file_path": hunk.file_path,
                "diff": hunk.diff_context,
                "question": "Why was this change made?",
                "answer_text": reply_text,
                "local_verdict": reply_verdict,
            }
        )

        # Dependency question: only for a hunk that actually touches a
        # symbol call_map_data records real callers for (mirrors
        # app/services/call_map.py's own matching — file + name-appears-
        # in-diff-text — so this only fires where the app itself would
        # have injected the same fact). Continues the same conversation
        # (history already has narration+reply in it) as a natural third
        # turn, rather than starting fresh.
        # Off while the app doesn't hand the call map to the model (see
        # vscode/TODO.md): the reply would have no recorded fact to repeat.
        symbol = _matching_symbol(call_map_data, hunk) if call_map_data and _ASK_DEPENDENCY else None
        if symbol:
            question = f"What else in this codebase calls `{symbol['name']}`?"
            t0 = time.time()
            history, dep_reply = conversation.respond_to_reviewer(history, question)
            timing[f"dependency[{symbol['name']}]"] = time.time() - t0

            callers_list = "\n".join(f"- {c['name']} ({c['file']})" for c in symbol["callers"]) or "(none recorded)"
            dep_verdict = judge_and_record(
                category="dependency",
                client=judge,
                system_prompt=DEPENDENCY_JUDGE_SYSTEM,
                user_prompt=DEPENDENCY_JUDGE_USER.format(
                    symbol_name=symbol["name"],
                    file_path=symbol["file"],
                    callers_list=callers_list,
                    human_text=question,
                    reply_text=dep_reply,
                ),
                context={
                    "symbol_name": symbol["name"],
                    "file_path": symbol["file"],
                    "callers_list": callers_list,
                    "human_text": question,
                    "reply_text": dep_reply,
                },
                k=JUDGE_VOTES,
            )
            entries.append(
                {
                    "category": "dependency",
                    "file_path": symbol["file"],
                    "diff": hunk.diff_context,
                    "question": question,
                    "answer_text": dep_reply,
                    "local_verdict": dep_verdict,
                }
            )

    # Explore mode: several real unchanged files with different questions,
    # same "ask about it" flow as qa_agent/live/test_live_explore_mode.py,
    # direct calls instead of a browser session per file. Filtered to
    # files with substantial content (qa_agent/live's own run already
    # covered the trivial-empty-file case, e.g. __init__.py — this batch
    # is deliberately looking for different signal, not repeating that
    # finding) — the substance filter comes first, THEN pick up to 3.
    from app.services.diff_service import list_all_files
    from app.services.editor_service import read_current_file

    changed_files = {h.file_path for h in hunks}
    explore_questions = [
        "What does this file do?",
        "What would break if this file were deleted?",
        "What does this file depend on?",
    ]
    substantial_unchanged = []
    for f in list_all_files(str(target_repo)):
        if f in changed_files:
            continue
        content = read_current_file(str(target_repo), f) or ""
        if len(content.strip()) > 200:
            substantial_unchanged.append((f, content))
        if len(substantial_unchanged) >= len(explore_questions):
            break

    for (unchanged, file_content), question in zip(substantial_unchanged, explore_questions, strict=False):
        t0 = time.time()
        _, explore_reply = conversation.answer_about_file([], unchanged, file_content, question)
        timing[f"explore[{unchanged}]"] = time.time() - t0

        verdict = judge_and_record(
            category="explore_reply",
            client=judge,
            system_prompt=EXPLORE_REPLY_JUDGE_SYSTEM,
            user_prompt=EXPLORE_REPLY_JUDGE_USER.format(
                file_path=unchanged,
                file_content=file_content,
                human_text=question,
                reply_text=explore_reply,
            ),
            context={
                "file_path": unchanged,
                "file_content": file_content,
                "human_text": question,
                "reply_text": explore_reply,
            },
            k=JUDGE_VOTES,
        )
        entries.append(
            {
                "category": "explore_reply",
                "file_path": unchanged,
                "diff": None,
                "question": question,
                "answer_text": explore_reply,
                "local_verdict": verdict,
            }
        )

    # Project-level question: relies entirely on the digest
    # project_context_for injects — only answerable now that a real
    # project_overview.json exists for target_repo (see conversation
    # history: this was blocked until that skill was run by hand).
    project_hunk = hunks[0]
    context = project_context_for(target_repo, hunks, project_hunk)
    briefing = briefing_client.analyze_hunk(project_hunk, conversation)
    history, _ = conversation.present_hunk([], project_hunk, briefing, context)
    t0 = time.time()
    _, project_reply = conversation.respond_to_reviewer(history, "What is this project, in a sentence?")
    timing["project_question"] = time.time() - t0
    entries.append(
        {
            "category": "reply",
            "file_path": project_hunk.file_path,
            "diff": project_hunk.diff_context,
            "question": "What is this project, in a sentence?",
            "answer_text": project_reply,
            "local_verdict": None,
        }
    )

    return {"entries": entries, "repo": target_repo}


def _write_pack_json(walk: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    repo_name = walk["repo"].name
    path = RESULTS_DIR / f"{repo_name}_direct_live_review_pack.json"
    items = []
    for i, entry in enumerate(walk["entries"]):
        item = {"index": i, **entry}
        item["content_hash"] = _item_content_hash(item)
        items.append(item)
    data = {
        "repo": repo_name,
        "target_repo": str(walk["repo"]),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "error_count": 0,
        "items": items,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def test_direct_walk_produced_answers(direct_walk):
    assert direct_walk["entries"], "no entries collected"
    empty = [e["file_path"] for e in direct_walk["entries"] if not (e["answer_text"] or "").strip()]
    assert not empty, f"empty answer for: {empty}"


def test_project_question_answered(direct_walk):
    project_entries = [e for e in direct_walk["entries"] if e["question"] == "What is this project, in a sentence?"]
    assert project_entries, "project-level question entry missing"
    assert project_entries[0]["answer_text"].strip()


def test_pack_is_written(direct_walk):
    path = _write_pack_json(direct_walk)
    print(f"\ndirect-call pack written to {path} — run judge-live-review against it")
    assert path.exists() and path.stat().st_size > 0


def test_print_timing(timing):
    """Not an assertion — this test's whole job is printing the evidence
    for whether direct calls are actually quicker, run with -s to see it."""
    total = sum(timing.values())
    print(f"\n--- direct-call timing ({len(timing)} LLM call(s), {total:.1f}s total) ---")
    for label, seconds in timing.items():
        print(f"  {seconds:6.1f}s  {label}")
    assert timing
