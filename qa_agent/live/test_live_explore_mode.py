"""Explore mode ("All files" toggle) against target_repo's real folder
tree — see qa_agent/test_explore_mode.py for the synthetic-repo version
this mirrors. The thing a real repo actually adds here: the scratch/
generated repos are shallow by construction (a handful of seeded files),
so this is the one place in this whole project that exercises a real,
deep, pre-existing folder structure and real unchanged file content rather
than something built to be easy to browse.

Unlike test_explore_mode.py's fixed `unchanged.py`, there is no known file
name to filter by here — target_repo's unchanged files are whatever that
project happens to have. Locators below take "the Nth .file-pill-unchanged"
rather than one by name.

Two of the tests below (the judged ones) also do what
qa_agent/test_semantic_quality.py does for changed hunks, but for
UNCHANGED files: ask a real question, capture the reply, and judge it
against the file's real content with EXPLORE_REPLY_JUDGE_* — grounding,
not diff-consistency (there is no diff for an unchanged file; see that
prompt pair's own comment in judge_prompts.py for why REPLY_JUDGE_* would
be miscalibrated here). Same "record, don't hard-assert on verdict
content" convention as the rest of this suite.
"""

from __future__ import annotations

from qa_agent.judge_prompts import EXPLORE_REPLY_JUDGE_SYSTEM, EXPLORE_REPLY_JUDGE_USER
from qa_agent.judging import judge_and_record
from qa_agent.llm_client import LLMClient
from qa_agent.ws_capture import WsFrames

from .conftest import _suppress_auto_tour

# A real unchanged file is bigger and slower to reason about than a seeded
# one-liner — same reasoning as test_live_review.py's REPLY_TIMEOUT_MS.
REPLY_TIMEOUT_MS = 60_000

# k=1, not judge_and_record's own k=3 default — see test_live_review.py's
# JUDGE_VOTES for the full reasoning (this is a free baseline
# judge-live-review re-checks afterward, not a standalone verdict that
# needs self-consistency voting to be trustworthy on its own).
JUDGE_VOTES = 1


def test_explore_toggle_lists_a_real_unchanged_file(page):
    page.click("#file-sidebar-tab")  # expand the file explorer sidebar
    assert page.locator(".file-pill-unchanged").count() == 0, "unchanged files shouldn't show before the toggle is on"

    page.click("#explore-all-files-btn")
    page.wait_for_selector(".file-pill-unchanged", timeout=15000)
    assert page.locator(".file-pill-unchanged").count() > 0, (
        "target_repo has no unchanged tracked files to list — expected for a repo with only new/untracked files"
    )
    assert page.locator("#explore-all-files-btn").get_attribute("aria-pressed") == "true"


def test_opening_a_real_unchanged_file_shows_its_real_content(page):
    page.click("#file-sidebar-tab")
    page.click("#explore-all-files-btn")
    page.wait_for_selector(".file-pill-unchanged", timeout=15000)

    pill = page.locator(".file-pill-unchanged").first
    file_name = pill.inner_text().strip()
    pill.click()
    page.wait_for_selector("#back-btn:not(.hidden)", timeout=15000)

    # hunk-meta names the file being shown; code-view renders its real
    # content — both read from disk, not seeded, so the only thing worth
    # asserting is that something non-trivial actually loaded.
    assert file_name in page.locator("#hunk-meta").inner_text()
    code_text = page.locator("#code-view").inner_text()
    assert code_text.strip(), f"{file_name} loaded with empty content"

    page.click("#back-btn")
    page.wait_for_selector("#back-btn", state="hidden", timeout=15000)


def _ask_about_unchanged_file(app_server, playwright, pill_index: int, question: str) -> dict:
    """Opens the Nth unchanged file, asks it a question, judges the reply
    for groundedness against that file's real content, and returns
    {file_name, file_content, question, reply_text, verdict}."""
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _suppress_auto_tour(page)
    frames = WsFrames()
    frames.attach(page)
    try:
        page.goto(app_server, wait_until="load")
        page.wait_for_selector("#status-bar span", timeout=15000)
        page.click("#file-sidebar-tab")
        page.click("#explore-all-files-btn")
        page.wait_for_selector(".file-pill-unchanged", timeout=15000)

        pills = page.locator(".file-pill-unchanged")
        assert pills.count() > pill_index, (
            f"target_repo has only {pills.count()} unchanged file(s), need at least {pill_index + 1}"
        )
        pill = pills.nth(pill_index)
        file_name = pill.inner_text().strip()
        pill.click()
        page.wait_for_selector("#back-btn:not(.hidden)", timeout=15000)
        file_content = page.locator("#code-view").inner_text()

        page.fill("#text-input", question)
        page.click("#send-btn")
        reply = frames.wait_for("reviewer_turn", page, timeout_ms=REPLY_TIMEOUT_MS)
        reply_text = reply.get("text", "")

        return {"file_name": file_name, "file_content": file_content, "question": question, "reply_text": reply_text}
    finally:
        browser.close()


def test_asking_about_a_real_unchanged_file_is_grounded(app_server, playwright, judge_model_config):
    result = _ask_about_unchanged_file(app_server, playwright, pill_index=0, question="What does this file do?")
    assert result["reply_text"].strip(), "explore-mode conversation must work before Start Review"

    verdict = judge_and_record(
        category="explore_reply",
        client=LLMClient(**judge_model_config),
        system_prompt=EXPLORE_REPLY_JUDGE_SYSTEM,
        user_prompt=EXPLORE_REPLY_JUDGE_USER.format(
            file_path=result["file_name"],
            file_content=result["file_content"],
            human_text=result["question"],
            reply_text=result["reply_text"],
        ),
        context={
            "file_path": result["file_name"],
            "file_content": result["file_content"],
            "human_text": result["question"],
            "reply_text": result["reply_text"],
        },
        k=JUDGE_VOTES,
    )
    print(f"\nexplore_reply verdict for {result['file_name']!r}: {verdict['verdict']} — {verdict['reason']}")
    assert verdict.get("verdict"), "no judge verdict recorded for the explore-mode reply"


def test_asking_a_different_question_about_a_different_file_is_grounded(app_server, playwright, judge_model_config):
    """A second, differently-shaped question against a different real file
    — one judged item isn't enough signal to say explore-mode grounding
    works in general, only that it worked once for one file/question
    pair."""
    result = _ask_about_unchanged_file(
        app_server, playwright, pill_index=1, question="What would break if this file were deleted?"
    )
    assert result["reply_text"].strip(), "explore-mode conversation must work before Start Review"

    verdict = judge_and_record(
        category="explore_reply",
        client=LLMClient(**judge_model_config),
        system_prompt=EXPLORE_REPLY_JUDGE_SYSTEM,
        user_prompt=EXPLORE_REPLY_JUDGE_USER.format(
            file_path=result["file_name"],
            file_content=result["file_content"],
            human_text=result["question"],
            reply_text=result["reply_text"],
        ),
        context={
            "file_path": result["file_name"],
            "file_content": result["file_content"],
            "human_text": result["question"],
            "reply_text": result["reply_text"],
        },
        k=JUDGE_VOTES,
    )
    print(f"\nexplore_reply verdict for {result['file_name']!r}: {verdict['verdict']} — {verdict['reason']}")
    assert verdict.get("verdict"), "no judge verdict recorded for the explore-mode reply"
