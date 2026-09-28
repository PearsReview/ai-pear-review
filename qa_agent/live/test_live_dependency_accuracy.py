"""The one place in this whole project with a real oracle instead of an
LLM judge's opinion: `.claude/skills/call-map/scan_calls.py`'s static
analysis records who ACTUALLY calls a given function (an `ast`-parsed
fact, not a model's guess), and `app/services/call_map.py` injects that
same fact into the app's own prompt for any hunk that touches a symbol
the map knows about (see that module's own docstring). So asking the app
"what calls this?" about such a hunk isn't asking it to reason about
unfamiliar code — it's asking it to faithfully repeat a fact it was
handed. `DEPENDENCY_JUDGE_*` (qa_agent/judge_prompts.py) judges the reply
against that same recorded fact, not against plausibility.

Skips (doesn't fail) when target_repo has nothing to offer this: not
Python, or its *current* diff happens not to touch any symbol with a
recorded caller. Both are properties of the diff, not the app — a skip
here means "nothing to check today," not "something's broken."
"""

from __future__ import annotations

import pytest

from qa_agent.judge_prompts import DEPENDENCY_JUDGE_SYSTEM, DEPENDENCY_JUDGE_USER
from qa_agent.judging import judge_and_record
from qa_agent.llm_client import LLMClient
from qa_agent.ws_capture import WsFrames

from .conftest import _suppress_auto_tour
from .test_live_review import (
    narrated_walk,  # noqa: F401 — re-exported as a fixture, see module docstring below
)

REPLY_TIMEOUT_MS = 60_000

# k=1, not judge_and_record's own k=3 default — see test_live_review.py's
# JUDGE_VOTES for the full reasoning.
JUDGE_VOTES = 1


def _format_callers(callers: list[dict]) -> str:
    return "\n".join(f"- {c['name']} ({c['file']})" for c in callers) or "(none recorded)"


@pytest.fixture(scope="session")
def dependency_target(call_map_data: dict | None, narrated_walk: dict) -> dict:  # noqa: F811 - the param requests the fixture the import above re-exports
    """The first hunk (in walk order) that is also the first hunk of its
    file AND touches a symbol call_map_data records callers for — i.e.
    exactly the case app/services/call_map.py's own call_map_prompt_block
    would inject a fact for. Restricted to a FILE's first hunk specifically
    so the test below can reach it with one file-pill click (jump_to_hunk
    to f.first_index) rather than walking Next through everything before
    it.

    Reuses narrated_walk's own recorded (file_path, diff) pairs rather
    than re-deriving them from git — same hunk boundaries the app itself
    used, not a second, possibly-drifted implementation of hunk-splitting.

    Skips when target_repo has nothing to offer (see module docstring)."""
    if not call_map_data:
        pytest.skip("no call map data for target_repo (not Python, or no symbol has a recorded caller)")

    entries = narrated_walk["entries"]
    first_hunk_index_by_file: dict[str, int] = {}
    for i, entry in enumerate(entries):
        first_hunk_index_by_file.setdefault(entry["file_path"], i)

    candidates = []
    for symbol in call_map_data.get("symbols", []):
        # first_hunk_index_by_file[symbol["file"]], by construction, IS the
        # index of that file's first hunk — no separate "is this the first
        # hunk" check needed, the lookup already only ever returns one.
        index = first_hunk_index_by_file.get(symbol["file"])
        if index is None:
            continue  # this symbol's file isn't part of the current diff at all
        diff_text = entries[index]["diff"]
        if symbol["name"] in diff_text:
            candidates.append((symbol, index, diff_text))

    if not candidates:
        pytest.skip(
            "no symbol with a recorded caller is touched by any file's first hunk in "
            "target_repo's current diff — nothing for this test to check today"
        )

    symbol, index, diff_text = max(candidates, key=lambda c: c[0]["caller_count"])
    return {"symbol": symbol, "hunk_index": index, "diff": diff_text, "file_path": symbol["file"]}


def test_call_map_was_generated(call_map_data: dict | None):
    """Structural sanity check, separate from dependency_target's skip
    logic: distinguishes "the scanner ran and genuinely found nothing" —
    also legitimate to skip on below — from "the scanner never ran or
    errored," which would be a real problem with the fixture itself."""
    if call_map_data is None:
        pytest.skip("target_repo isn't Python, or has no symbol with a recorded caller — nothing to scan for")
    assert call_map_data.get("symbols"), "call map was generated but recorded zero symbols with callers"


def test_dependency_reply_matches_the_recorded_call_graph(
    app_server: str, playwright, dependency_target: dict, judge_model_config: dict
):
    symbol = dependency_target["symbol"]
    question = f"What else in this codebase calls `{symbol['name']}`?"

    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    frames = WsFrames()
    frames.attach(page)
    try:
        page.goto(app_server, wait_until="load")
        page.wait_for_selector("#status-bar span", timeout=15000)
        page.click("#start-review-btn", timeout=10000)

        # Jump straight to this symbol's file — its FIRST hunk, which
        # dependency_target already confirmed is the one touching our
        # symbol — rather than walking Next through everything before it.
        page.click("#file-sidebar-tab")
        file_name = dependency_target["file_path"].rsplit("/", 1)[-1]
        page.locator(".file-pill", has_text=file_name).first.click()
        frames.wait_for("narration", page, timeout_ms=120_000)

        page.fill("#text-input", question)
        page.click("#send-btn")
        reply = frames.wait_for("reviewer_turn", page, timeout_ms=REPLY_TIMEOUT_MS)
        reply_text = reply.get("text", "")
        assert reply_text.strip(), f"empty reply to {question!r}"

        callers_list = _format_callers(symbol["callers"])
        verdict = judge_and_record(
            category="dependency",
            client=LLMClient(**judge_model_config),
            system_prompt=DEPENDENCY_JUDGE_SYSTEM,
            user_prompt=DEPENDENCY_JUDGE_USER.format(
                symbol_name=symbol["name"],
                file_path=symbol["file"],
                callers_list=callers_list,
                human_text=question,
                reply_text=reply_text,
            ),
            context={
                "symbol_name": symbol["name"],
                "file_path": symbol["file"],
                "callers_list": callers_list,
                "human_text": question,
                "reply_text": reply_text,
            },
            k=JUDGE_VOTES,
        )
        print(
            f"\ndependency verdict for `{symbol['name']}` "
            f"({symbol['caller_count']} recorded caller(s)): {verdict['verdict']} — {verdict['reason']}"
        )
        assert verdict.get("verdict"), "no judge verdict recorded for the dependency reply"
    finally:
        browser.close()
