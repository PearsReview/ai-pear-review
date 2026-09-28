"""Realistic *navigation* scenarios, each layered with a live conversation —
the gap qa_agent/test_conversation_dedup.py and test_navigation.py leave on
their own: those prove rapid-double-click disabling and revisit-dedup work
in isolation, but never combine either with an actual reply in flight, and
never exercise more than the scratch repo's single default hunk. See
qa_agent/README.md's "Stage 3" section for how this differs from the
original (still-deferred) "agent decides its own actions" design.

test_switch_hunks_mid_reply_... and test_revisit_one_of_several_hunks_...
use richer_scratch_repo (conftest.py) rather than the plain scratch_repo —
they need genuinely more than one hunk/file to mean anything: under that
fixture there are 4 hunks across 3 changed files (sample.py, feature.py,
src/utils.py) spread across 2 folders, alongside the always-present
zero-diff pkg/ and docs/ folders, so there's real ground to click across
files and folders rather than just one hunk in one file. The other two
scenarios here work fine against the plain single-hunk scratch_repo, same
as every Stage 1/2 test.
"""

from __future__ import annotations

from ..judge_prompts import REPLY_JUDGE_SYSTEM, REPLY_JUDGE_USER
from ..judging import judge_and_record
from ..llm_client import LLMClient
from .scenario_helpers import ask_and_capture_reply, tts_status_frame_count, wait_for_new_frame

# A hunk's first narration is two chained model calls, not one — a fresh
# briefing (no .briefing/ cache entry yet) and then the narration itself —
# measured live on this machine at up to ~40s combined (briefing ~10-20s +
# narrate ~5-25s). 30s was the original value here and timed out for real
# in three separate tests across this session before every narration wait
# in the file was moved onto this one constant. Matches the suite's own
# existing precedent (test_act_now.py's ACT_NOW_TIMEOUT_MS = 45_000) of
# giving a real model call realistic headroom rather than a timeout
# calibrated to the fast/cached case.
_FRESH_NARRATION_TIMEOUT_MS = 60_000


def test_conversation_survives_double_click_next_prev(page_with_ws):
    page, frames = page_with_ws
    frames.wait_for("narration", page, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)
    page.wait_for_timeout(500)

    ask_and_capture_reply(page, frames, "Why was this change made?")
    # .turn.presenter covers every AI turn, not narration alone — the
    # persona speaking as "Author (Claude)" either way, whether it's the
    # initial narration or a reply to a question. .turn.reviewer is the
    # human's own typed turn. So after one reply there are legitimately 2
    # presenter turns (narration + reply), not 1 — verified live: an
    # earlier version of this test asserted count() == 1 here and failed,
    # which is what revealed what the class naming actually means.
    presenter_turns_after_reply = page.locator("#transcript .turn.presenter").count()
    assert presenter_turns_after_reply == 2, "expected the initial narration plus one reply"
    assert page.locator("#transcript .turn.reviewer").count() == 1

    # Next/Prev disable themselves immediately on click and only re-enable
    # once the response lands (see static/js/status.js's reenableActionButtons
    # doc; test_navigation.py's own test_next_btn_disables_immediately_and_
    # reenables already proves that specific guard, via a raw JS click that
    # bypasses Playwright's own actionability wait — page.click() itself
    # can't press a disabled button, so back-to-back page.click() calls
    # here are naturally serialized into "click, wait for re-enable, click
    # again" rather than truly racing the disabled state). What this test
    # adds on top: repeated quick navigation while a conversation is
    # already in the transcript, and confirming that conversation survives
    # a revisit intact rather than getting duplicated or dropped.
    page.click("#next-btn")
    page.click("#next-btn")  # only one hunk exists here — "done" is idempotent past the end
    page.wait_for_timeout(400)
    page.click("#prev-btn")
    page.click("#prev-btn")  # -> back to the only real hunk, a cache hit
    page.wait_for_timeout(500)

    assert "sample.py" in page.locator("#hunk-meta").inner_text()
    assert page.locator("#transcript .turn.presenter").count() == presenter_turns_after_reply, (
        "revisiting the same hunk via repeated Next/Prev must not add a duplicate presenter turn"
    )
    assert page.locator("#transcript .turn.reviewer").count() == 1, (
        "the earlier reviewer question must still be visible in the transcript, not lost"
    )
    # And the conversation must still be usable afterward, not left stuck
    # disabled by the repeated clicking.
    assert not page.locator("#send-btn").is_disabled()


def test_switch_hunks_mid_reply_clears_stale_ui_and_conversation_still_works(
    richer_scratch_repo, page_with_ws, judge_model_config, llm_capture_dir
):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    frames.wait_for("narration", page, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)

    # Open an inline-comment composer on the current hunk but never send it.
    first_line = page.locator(".code-line").first
    first_line.hover()  # .line-comment-add is opacity:0/pointer-events:none until hovered
    first_line.locator(".line-comment-add").click()
    page.wait_for_timeout(150)
    assert page.locator(".line-comment-composer").count() == 1, "composer should be open before switching"

    # Jump to a DIFFERENT file's hunk via the sidebar — a real reviewer
    # clicking a different file mid-draft, not a synthesized WS message.
    # richer_scratch_repo has 3 changed files (sample.py, feature.py,
    # src/utils.py); which one auto-presents first on connect isn't
    # something this test should assume, so target_file just needs to be
    # *some* file other than whichever one that turned out to be — always
    # true of "feature.py" unless that's the one already open, in which
    # case "sample.py" is.
    current_file = presenting["file_path"]
    target_file = "feature.py" if current_file != "feature.py" else "sample.py"
    page.click("#file-sidebar-tab")
    start_index = len(frames.frames)
    page.locator(".file-pill").filter(has_text=target_file).first.click()
    new_presenting = wait_for_new_frame(frames, page, "presenting", start_index, timeout_ms=15_000)
    assert new_presenting["file_path"] == target_file

    # Everything onPresenting (review-flow.js) documents clearing on any real
    # hunk/file switch.
    assert page.locator(".line-comment-composer").count() == 0, (
        "switching files must close an unsent inline-comment composer"
    )
    assert page.locator("#act-now-confirm-row.hidden").count() == 1
    assert page.locator("#explore-all-files-btn").get_attribute("aria-pressed") == "false"

    # And the conversation on the NEW file's hunk still works normally —
    # switching mid-draft must not leave the composer/session half-broken.
    #
    # Wait for the *new* hunk's narration before asking. Each hunk has its
    # own conversation history, empty until that hunk's narration lands,
    # and the diff only enters it via narration's own prompt — so asking
    # the moment "presenting" arrives hands the model nothing but the bare
    # question. That produced a recorded "no" here (a confident answer
    # about "optimizing performance by reducing iterations" for a diff
    # that adds a farewell function), caught by the llm_call capture
    # showing messages == [{"role": "user", "content": "Why was this
    # change made?"}] — the same trap test_semantic_quality.py's
    # test_reply_makes_sense had. wait_for_new_frame, not frames.wait_for:
    # hunk 0's narration is already in frames by now.
    wait_for_new_frame(frames, page, "narration", start_index, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)

    question = "Why was this change made?"
    reviewer_turn = ask_and_capture_reply(page, frames, question)

    judge_and_record(
        category="reply",
        client=LLMClient(**judge_model_config),
        system_prompt=REPLY_JUDGE_SYSTEM,
        user_prompt=REPLY_JUDGE_USER.format(
            file_path=new_presenting["file_path"],
            diff=new_presenting["diff"],
            human_text=question,
            reply_text=reviewer_turn["text"],
        ),
        context={
            "file_path": new_presenting["file_path"],
            "diff": new_presenting["diff"],
            "human_text": question,
            "reply_text": reviewer_turn["text"],
        },
        response_text=reviewer_turn["text"],
        capture_dir=llm_capture_dir,
    )


def test_interrupt_then_new_question_gets_a_real_answer(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws
    presenting = frames.wait_for("presenting", page)
    frames.wait_for("narration", page, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)

    interrupted_question = "Why was this change made?"
    start_index = len(frames.frames)
    page.fill("#text-input", interrupted_question)
    page.click("#send-btn")
    page.wait_for_timeout(150)  # give the request a moment to actually be in flight before cancelling it
    page.click("#interrupt-btn")

    # Bounded window to (incorrectly) still produce a reply anyway, then
    # confirm it didn't — proving the negative within a timeout, same shape
    # as _wait_for_act_now_result's own timeout in test_semantic_quality.py,
    # just asserting absence instead of presence.
    page.wait_for_timeout(3000)
    late_replies = [f for f in frames.frames[start_index:] if f.get("type") == "reviewer_turn"]
    assert late_replies == [], "an interrupted question must not still produce a reviewer_turn reply"

    # The session must have actually recovered, not be left half-cancelled —
    # a second, real question gets a real, judged answer.
    recovery_question = "What does this hunk change?"
    reviewer_turn = ask_and_capture_reply(page, frames, recovery_question)

    judge_and_record(
        category="reply",
        client=LLMClient(**judge_model_config),
        system_prompt=REPLY_JUDGE_SYSTEM,
        user_prompt=REPLY_JUDGE_USER.format(
            file_path=presenting["file_path"],
            diff=presenting["diff"],
            human_text=recovery_question,
            reply_text=reviewer_turn["text"],
        ),
        context={
            "file_path": presenting["file_path"],
            "diff": presenting["diff"],
            "human_text": recovery_question,
            "reply_text": reviewer_turn["text"],
        },
        response_text=reviewer_turn["text"],
        capture_dir=llm_capture_dir,
    )


def test_revisit_one_of_several_hunks_no_duplicate_or_replay(richer_scratch_repo, page_with_ws):
    page, frames = page_with_ws
    frames.wait_for("narration", page, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)  # hunk 0's initial narration
    page.wait_for_timeout(500)

    # wait_for_new_frame (not frames.wait_for directly) for every narration
    # after the first: frames.wait_for returns the newest EXISTING match
    # the instant any match exists at all — called again here it would
    # immediately return hunk 0's already-captured narration instead of
    # actually waiting for hunk 1's/hunk 2's, since a "narration" frame is
    # already sitting in frames.frames by then. See scenario_helpers.py's
    # wait_for_new_frame docstring for the same reasoning applied to
    # multi-turn replies.
    since_index = len(frames.frames)
    page.click("#next-btn")  # -> hunk 1
    wait_for_new_frame(frames, page, "narration", since_index, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)
    page.wait_for_timeout(300)

    since_index = len(frames.frames)
    page.click("#next-btn")  # -> hunk 2
    wait_for_new_frame(frames, page, "narration", since_index, timeout_ms=_FRESH_NARRATION_TIMEOUT_MS)
    page.wait_for_timeout(300)

    assert page.locator("#transcript .turn.presenter").count() == 3, (
        "expected one presenter turn per distinct hunk (0, 1, 2) visited so far"
    )
    tts_frames_before_revisit = tts_status_frame_count(frames)

    page.click("#prev-btn")  # -> back to hunk 1, a cache hit — the multi-hunk case
    # test_conversation_dedup.py's own single-hunk repo structurally can't exercise.
    page.wait_for_timeout(500)

    assert page.locator("#transcript .turn.presenter").count() == 3, (
        "revisiting hunk 1 via Prev must not add a duplicate conversation turn"
    )
    assert tts_status_frame_count(frames) == tts_frames_before_revisit, (
        "revisiting hunk 1 via Prev must not re-trigger TTS"
    )
