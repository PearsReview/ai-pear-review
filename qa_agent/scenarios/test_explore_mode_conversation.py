"""Explore-mode conversation scenario (Stage 3, see qa_agent/README.md) —
asking a question about an unchanged file goes through a structurally
different prompt path than a hunk-scoped reply (answer_about_file, the
whole file embedded, explicitly told the file "has NOT changed" — see
app/services/conversation_service.py), so it gets its own judge rubric
rather than reusing REPLY_JUDGE_* verbatim, whose rubric is framed around
diff-consistency and doesn't apply here. See judge_prompts.py's
EXPLORE_REPLY_JUDGE_* for the rubric this uses.

Reuses qa_agent/test_explore_mode.py's own selectors/pattern
(_unchanged_pill, #explore-all-files-btn) rather than reinventing them —
that file's own tests cover the mechanical UI behavior; this one adds the
LLM-judged "was the answer actually grounded in the file" question on top.
"""

from __future__ import annotations

from ..judge_prompts import EXPLORE_REPLY_JUDGE_SYSTEM, EXPLORE_REPLY_JUDGE_USER
from ..judging import judge_and_record
from ..llm_client import LLMClient
from .scenario_helpers import ask_and_capture_reply, wait_for_new_frame


def test_explore_mode_question_gets_a_grounded_reply(page_with_ws, judge_model_config, llm_capture_dir):
    page, frames = page_with_ws

    page.click("#file-sidebar-tab")
    page.click("#explore-all-files-btn")
    page.wait_for_selector(".file-pill-unchanged", timeout=15_000)

    start_index = len(frames.frames)
    page.locator(".file-pill-unchanged").filter(has_text="unchanged.py").click()
    file_explore = wait_for_new_frame(frames, page, "file_explore", start_index, timeout_ms=15_000)
    file_content = "\n".join(file_explore["lines"])

    question = "What does this file do?"
    reviewer_turn = ask_and_capture_reply(page, frames, question)

    judge_and_record(
        category="explore_reply",
        client=LLMClient(**judge_model_config),
        system_prompt=EXPLORE_REPLY_JUDGE_SYSTEM,
        user_prompt=EXPLORE_REPLY_JUDGE_USER.format(
            file_path=file_explore["file_path"],
            file_content=file_content,
            human_text=question,
            reply_text=reviewer_turn["text"],
        ),
        context={
            "file_path": file_explore["file_path"],
            "file_content": file_content,
            "human_text": question,
            "reply_text": reviewer_turn["text"],
        },
        response_text=reviewer_turn["text"],
        capture_dir=llm_capture_dir,
    )
