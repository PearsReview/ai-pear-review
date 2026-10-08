"""Act Now: a coding agent (see harness_service.py) proposes a change, shown as
a preview, and written only on explicit confirmation."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from dataclasses import replace
from functools import partial

from fastapi import WebSocket

from ..services.diff_service import diff_text_to_full_lines
from ..services.harness_service import (
    ActNowRequest,
    AgentEdit,
    HarnessError,
    ProposedChange,
    agent_label,
    apply_changes,
    build_act_now_prompt,
    harness_status,
    run_agent_edit,
)
from ..services.voice_service import VoiceServiceError
from ..web import runtime
from ..web.config import CONFIG
from ..web.context import line_context
from ..web.runtime import run_agent, send_agent_stopped, send_error, send_json
from ..web.session import Session
from .registry import handler
from .review_flow import refresh_diff

log = logging.getLogger("ai_pear_review")


@handler("act_now", cancels=True, background=True, writes=True)
async def handle_act_now(ws: WebSocket, session: Session, payload: dict) -> None:
    """Runs the configured agent on a copy of the repo and returns what it
    changed as "act_now_preview" — nothing is written to disk here (see
    handle_confirm_act_now for the only step that writes)."""
    if session.review_ended:
        # Browsing a hunk post-end can put a real hunk on screen again, so
        # this can't rely on the client alone. Act Now changes code, which
        # belongs to an open review; chat about the change still works.
        await send_error(ws, "The review has ended — reopen it to use Act Now.")
        return
    status = harness_status(CONFIG["harness"])
    if not status.available:
        await send_error(ws, status.detail)
        return

    hunk = session.current_hunk
    marked_lines = payload.get("marked_lines")
    if marked_lines:
        marked_file = marked_lines[0].get("file_path")
        hunk = next((h for h in session.hunks if h.file_path == marked_file), hunk)
    if hunk is None:
        await send_error(ws, "No hunk is currently being reviewed.")
        return

    instruction = payload.get("text")
    audio_b64 = payload.get("audio_base64")
    if not instruction and audio_b64:
        try:
            audio_bytes = base64.b64decode(audio_b64)
            instruction = await asyncio.to_thread(runtime.STT.transcribe, audio_bytes)
            await send_json(ws, "service_status", {"stt": True})
        except VoiceServiceError as exc:
            log.info("STT unavailable: %s", exc)
            await send_json(ws, "service_status", {"stt": False})
            await send_error(ws, "Voice input is unavailable right now — please type your request.")
            return
        except (binascii.Error, TypeError) as exc:
            # Same malformed-base64 case as handle_reply's identical guard —
            # this handler runs as a task, so an uncaught exception here would
            # die silently with no error frame sent.
            log.info("Malformed audio payload for act_now: %s", exc)
            await send_json(ws, "service_status", {"stt": False})
            await send_error(ws, "Couldn't process the recorded audio — please try again.")
            return
    if not instruction:
        await send_error(ws, "Didn't catch anything — please try again.")
        return

    file_path, where, snippet, _anchor = line_context(
        hunk, marked_lines
    )  # anchor is only meaningful for queued review comments
    request = ActNowRequest(file_path, where, snippet, (instruction,))
    try:
        edit = await _run(session, request, ())
    except asyncio.CancelledError:
        await send_agent_stopped(ws, "act_now", "Act Now was stopped — nothing was changed.")
        raise
    except HarnessError as exc:
        await send_error(ws, f"Act Now failed: {exc}")
        return
    if not await _send_preview(ws, session, request, edit):
        message = f"{agent_label(CONFIG['harness'])} didn't change any files."
        await send_json(ws, "notice", {"level": "info", "message": f"{message} {edit.summary}".strip()})


@handler("refine_act_now", cancels=True, background=True, writes=True)
async def handle_refine_act_now(ws: WebSocket, session: Session, payload: dict) -> None:
    """Re-runs the agent on top of the pending preview with the reviewer's
    follow-up, and replaces that preview with the result. A failure leaves
    the pending preview as it was, still there to apply or refine again."""
    if session.review_ended:
        await send_error(ws, "The review has ended — reopen it to use Act Now.")
        return
    status = harness_status(CONFIG["harness"])
    if not status.available:
        await send_error(ws, status.detail)
        return
    pending, request = session.pending_act_now, session.act_now_request
    if pending is None or request is None:
        await send_error(ws, "Nothing to refine — generate a preview first.")
        return
    text = (payload.get("text") or "").strip()
    if not text:
        await send_error(ws, "Say what to change about the proposal first.")
        return

    refined = replace(request, instructions=(*request.instructions, text))
    try:
        edit = await _run(session, refined, pending)
    except asyncio.CancelledError:
        # The pending preview is untouched: only _send_preview replaces it.
        await send_agent_stopped(ws, "refine_act_now", "Refine was stopped — the previous proposal is still there.")
        raise
    except HarnessError as exc:
        await send_error(ws, f"Refine failed: {exc}")
        return
    if not await _send_preview(ws, session, refined, edit):
        session.pending_act_now = session.act_now_request = None
        message = f"{agent_label(CONFIG['harness'])}'s refinement left no changes to apply."
        await send_json(ws, "act_now_cleared", {"message": f"{message} {edit.summary}".strip()})


async def _run(session: Session, request: ActNowRequest, base: tuple[ProposedChange, ...]) -> AgentEdit:
    return await run_agent(
        f"act_now {request.file_path}",
        # By keyword: run_agent appends the cancel token as the last positional argument.
        partial(run_agent_edit, base=base),
        CONFIG["harness"],
        session.repo_path,
        build_act_now_prompt(request),
    )


async def _send_preview(ws: WebSocket, session: Session, request: ActNowRequest, edit: AgentEdit) -> bool:
    """Makes edit the pending preview and sends it; False when there's
    nothing a reviewer could see."""
    files = [entry for entry in map(_preview_entry, edit.changes) if entry is not None]
    if not files:
        return False
    # Open on the file the reviewer asked about, then edits before additions/deletions.
    files.sort(key=lambda f: (f["file_path"] != request.file_path, ("modified", "added", "deleted").index(f["status"])))
    session.pending_act_now = edit.changes
    session.act_now_request = request
    await send_json(
        ws,
        "act_now_preview",
        # The first file's fields sit at the top level too, so the preview
        # opens on it without the client having to pick.
        {**files[0], "files": files, "summary": edit.summary, "agent": agent_label(CONFIG["harness"])},
    )
    return True


def _preview_entry(change: ProposedChange) -> dict | None:
    full_lines = diff_text_to_full_lines(change.old_text or "", change.new_text or "")
    if not full_lines and change.old_text is not None and change.new_text is not None:
        return None  # only line endings changed — nothing a reviewer can see
    # Highlight the changed lines, not the whole file, so the view scrolls to
    # the edit rather than the top of the file.
    changed = [i for i, line in enumerate(full_lines) if line["kind"] != "context"]
    return {
        "file_path": change.file_path,
        "status": "added" if change.old_text is None else "deleted" if change.new_text is None else "modified",
        "full_lines": full_lines,
        "highlight_start": min(changed) if changed else 0,
        "highlight_end": max(changed) if changed else max(len(full_lines) - 1, 0),
    }


@handler("confirm_act_now", writes=True)
async def handle_confirm_act_now(ws: WebSocket, session: Session, payload: dict) -> None:
    """Writes exactly what the last "act_now" previewed — never re-runs the
    agent, never trusts content from the client at this step.

    Success is a "notice" with "event": "act_now_applied" and the written
    "files", the one signal a client can clear its proposal on."""
    if session.review_ended:
        # A preview generated before End Review isn't cleared by ending, so a
        # stale confirm bar or a crafted request could otherwise still write
        # after the reviewer said the review was done.
        await send_error(ws, "The review has ended — reopen it to use Act Now.")
        return
    pending = session.pending_act_now
    if pending is None:
        await send_error(ws, "Nothing to confirm — generate a preview first.")
        return
    session.pending_act_now = session.act_now_request = None

    try:
        await asyncio.to_thread(apply_changes, session.repo_path, pending)
    except (HarnessError, OSError) as exc:
        await send_error(ws, f"Act Now change not applied: {exc}")
        return

    files = [change.file_path for change in pending]
    await send_json(
        ws,
        "notice",
        {
            "level": "success",
            "message": f"Applied the change to {', '.join(files)}.",
            "event": "act_now_applied",
            "files": files,
        },
    )
    await refresh_diff(ws, session)
