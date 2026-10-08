"""Look deeper: the reviewer's own coding agent investigates one question about
a hunk, read-only (see harness_service.run_agent_research)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocket

from ..services.harness_service import HarnessError, agent_label, harness_status, run_agent_research
from ..utils.markdown_speech import block_to_payload, sanitize_persona_reply
from ..web.config import CONFIG
from ..web.runtime import run_agent, send_agent_stopped, send_error, send_json
from ..web.session import Session
from ..web.speech import try_speak
from .registry import handler

log = logging.getLogger("ai_pear_review")

# Asked when the button sits under a narration, which answers no question.
_NARRATION_QUESTION = "Look deeper at this change: what it does, why it was made, and what else it affects."


@handler("look_deeper", cancels=True, background=True)
async def handle_look_deeper(ws: WebSocket, session: Session, payload: dict) -> None:
    """Sends one question about a hunk to the reviewer's coding agent, which
    reads the repository to answer it.

    Payload: {"index": hunk index, "question": the reviewer question the
    clicked reply answered — absent for a narration}. Replies with
    "deeper_turn": {text, blocks, index, total, file_path, agent, provider,
    model}, or "agent_stopped" if cancelled first (Interrupt, or any other
    cancels=True action).

    The answer carries no warning about which model produced it; that
    guidance belongs where the model is chosen, in
    harness_service.research_note.

    Read aloud like any other reply when voice output is on; try_speak
    splits it to fit config.yaml's tts.max_chars."""
    status = harness_status(CONFIG["harness"])
    if not status.available:
        await send_error(ws, status.detail)
        return
    index = payload.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(session.hunks):
        await send_error(ws, "That hunk is no longer part of the review — refresh and try again.")
        return
    hunk = session.hunks[index]
    question = payload.get("question")
    question = question.strip() if isinstance(question, str) and question.strip() else _NARRATION_QUESTION
    label = agent_label(CONFIG["harness"])

    try:
        result = await run_agent(
            f"look_deeper {hunk.file_path}",
            run_agent_research,
            CONFIG["harness"],
            session.repo_path,
            hunk.file_path,
            hunk.header,
            hunk.diff_context,
            question,
        )
    except asyncio.CancelledError:
        await send_agent_stopped(ws, "look_deeper", "Look deeper was stopped before it finished.")
        raise
    except HarnessError as exc:
        await send_error(ws, f"Look deeper failed: {exc}")
        return
    except Exception as exc:
        # A background task: anything uncaught here is swallowed by asyncio,
        # leaving the UI's "Looking deeper" indicator spinning forever.
        log.exception("look_deeper %s crashed", hunk.file_path)
        await send_error(ws, f"Look deeper failed unexpectedly: {exc}")
        return
    log.info(
        "look_deeper %s: %d tool calls, %d refused, model=%s",
        hunk.file_path,
        result.tool_calls,
        result.refused,
        result.model.model,
    )

    # Carried into the hunk's conversation so a later normal reply can build on
    # what was found. Only onto an existing history: an empty one is how the
    # next reply knows to prepend the diff (narration._with_diff_if_needed), so
    # seeding it here would strip that grounding from the follow-up.
    history = session.conversation_histories.get(index)
    if history:
        session.conversation_histories[index] = [
            *history,
            {"role": "user", "content": f"(Looked deeper with {label}, reading the repository) {question}"},
            {"role": "assistant", "content": result.answer},
        ]
    session.transcript.append({"role": "deeper", "text": result.answer})

    blocks, spoken = sanitize_persona_reply(result.answer, hunk.diff_context)
    await send_json(
        ws,
        "deeper_turn",
        {
            "text": result.answer,
            "blocks": [block_to_payload(b) for b in blocks],
            # For the turn's speaker button, same as narration's.
            "spoken": spoken,
            "index": index,
            "total": len(session.hunks),
            "file_path": hunk.file_path,
            "agent": label,
            "provider": result.model.provider,
            "model": result.model.model,
        },
    )
    # Read aloud like any other reply when voice output is on.
    await try_speak(ws, session, spoken)
