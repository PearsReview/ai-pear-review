"""Browsing outside the diff: step into a definition, list and open any
file, and ask about an opened file."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging

from fastapi import WebSocket

from ..services.code_search import DefinitionNotFound, find_definition
from ..services.conversation_service import ConversationError
from ..services.diff_service import DiffError, list_all_files
from ..services.editor_service import read_current_file
from ..services.voice_service import VoiceServiceError
from ..utils.markdown_speech import block_to_payload, sanitize_persona_reply
from ..web import runtime
from ..web.config import CONFIG
from ..web.context import estimate_tokens, prompt_budget
from ..web.runtime import run_llm, send_error, send_json
from ..web.session import Session
from ..web.speech import try_speak
from .registry import handler

log = logging.getLogger("ai_pear_review")


@handler("step_into")
async def handle_step_into(ws: WebSocket, session: Session, payload: dict) -> None:
    """Opens the definition of a selected identifier in the code view.

    Payload: {"text": str}, the identifier the reviewer selected. Replies
    with "definition".

    A peek at an arbitrary file the diff never touched, not a change of
    position in the walkthrough: session.index and session.hunks are
    untouched. The frontend remembers what to restore on Back, so there is
    no peek stack on the server."""
    identifier = (payload.get("text") or "").strip()
    if not identifier:
        await send_error(ws, "Nothing selected to step into.")
        return

    try:
        definition = await asyncio.to_thread(find_definition, CONFIG["server"].get("repo_path", "."), identifier)
    except DefinitionNotFound as exc:
        await send_error(ws, str(exc))
        return

    await send_json(
        ws,
        "definition",
        {
            "identifier": identifier,
            "file_path": definition.file_path,
            "line_number": definition.line_number,
            "context_start": definition.context_start,
            "lines": definition.lines,
        },
    )


@handler("list_all_files")
async def handle_list_all_files(ws: WebSocket, session: Session, payload: dict) -> None:
    """Lists every file in the working tree, for the sidebar's "All files"
    toggle.

    No payload. The whole tree (see list_all_files in diff_service.py) is
    merged with the changed-file entries send_review_progress already
    computes, so the client gets one list rather than two to reconcile
    itself.

    A file with hunks carries {file_path, hunk_count, reviewed_count,
    first_index}; every other file gets hunk_count: null, which is how the
    client decides to send "explore_file" rather than "jump_to_hunk" (see
    renderFileTreeNode in sidebar.js).

    Not gated on review_started or review_ended — browsing works at any
    point, the same as hunk navigation."""
    try:
        paths = await asyncio.to_thread(list_all_files, session.repo_path)
    except DiffError as exc:
        await send_error(ws, f"Could not list files: {exc}")
        return

    changed: dict[str, dict] = {}
    for i, hunk in enumerate(session.hunks):
        entry = changed.setdefault(
            hunk.file_path,
            {"file_path": hunk.file_path, "hunk_count": 0, "reviewed_count": 0, "first_index": i},
        )
        entry["hunk_count"] += 1
        if i in session.reviewed:
            entry["reviewed_count"] += 1

    files = [
        changed.get(path) or {"file_path": path, "hunk_count": None, "reviewed_count": None, "first_index": None}
        for path in paths
    ]
    # A changed file that list_all_files somehow didn't surface (a staged
    # deletion still in session.hunks but no longer in `git ls-files`, the
    # one edge case worth not silently dropping) — appended rather than
    # lost, so "All files" mode is always a strict superset of the
    # changed-files list, never a subset of it.
    covered = {f["file_path"] for f in files}
    files.extend(entry for path, entry in changed.items() if path not in covered)
    await send_json(ws, "all_files", {"files": files})


@handler("explore_file")
async def handle_explore_file(ws: WebSocket, session: Session, payload: dict) -> None:
    """Opens any file from the "All files" sidebar, read-only, in the code
    view.

    Payload: {"file_path": str}. Replies with "file_explore".

    The same peek mechanism as handle_step_into above, entered by path from
    the sidebar rather than by matching a selected identifier, and showing
    the whole file rather than a window around one line. session.index and
    session.hunks are untouched, and the frontend remembers what to restore
    on Back (see state.savedHunkView in static/js/state.js).

    Not gated on review_started or review_ended — reading an unchanged file
    works at any point, including before a review has started."""
    file_path = (payload.get("file_path") or "").strip()
    if not file_path:
        await send_error(ws, "No file selected.")
        return

    content = await asyncio.to_thread(read_current_file, session.repo_path, file_path)
    if content is None:
        await send_error(ws, f"Couldn't read {file_path}.")
        return

    await send_json(ws, "file_explore", {"file_path": file_path, "lines": content.splitlines()})


@handler("explore_reply", cancels=True, background=True)
async def handle_explore_reply(ws: WebSocket, session: Session, payload: dict) -> None:
    """Answers a question about a file opened through explore mode.

    Payload: {"text": ...} or {"audio_base64": ...}, plus "file_path". The
    same shape as handle_reply, but about a file opened by
    handle_explore_file rather than about the current hunk — see
    Session.file_conversation_histories and
    ConversationClient.answer_about_file.

    Marked-line context is not threaded through here the way handle_reply
    does it with augment_with_marked_context: line marking belongs to hunk
    review, not to explore mode.

    There is no review_started or review_ended check either. Asking about
    unchanged code works before a review starts, unlike
    handle_request_change, which does gate on it — queuing a comment is a
    review action, a question about unrelated code isn't."""
    file_path = (payload.get("file_path") or "").strip()
    if not file_path:
        await send_error(ws, "No file selected.")
        return

    if session.conversation is None:
        await send_error(ws, "Conversation agent unavailable — check it's configured correctly to enable replies.")
        return

    human_text = payload.get("text")
    audio_b64 = payload.get("audio_base64")
    from_voice = not human_text and bool(audio_b64)

    if not human_text and audio_b64:
        try:
            audio_bytes = base64.b64decode(audio_b64)
            human_text = await asyncio.to_thread(runtime.STT.transcribe, audio_bytes)
            await send_json(ws, "service_status", {"stt": True})
        except VoiceServiceError as exc:
            log.info("STT unavailable: %s", exc)
            await send_json(ws, "service_status", {"stt": False})
            await send_error(ws, "Voice input is unavailable right now — please type your question.")
            return
        except (binascii.Error, TypeError) as exc:
            # Same malformed-base64 case as handle_reply's identical guard.
            log.info("Malformed audio payload for explore_reply: %s", exc)
            await send_json(ws, "service_status", {"stt": False})
            await send_error(ws, "Couldn't process the recorded audio — please try again.")
            return

    if not human_text:
        await send_error(ws, "Didn't catch anything — please try again.")
        return

    content = await asyncio.to_thread(read_current_file, session.repo_path, file_path)
    if content is None:
        await send_error(ws, f"Couldn't read {file_path}.")
        return

    # Explore mode embeds the WHOLE file on the first turn (see
    # ConversationClient._file_prompt), so this is the one path where a
    # single click can exceed the window outright — a reviewer browsing
    # "all files" can land on anything. Check before spending the call:
    # over-budget here doesn't degrade gracefully, it front-truncates
    # silently and answers about whatever tail survived.
    budget = prompt_budget(session)
    file_tokens = estimate_tokens(content)
    if (
        budget is not None
        and file_tokens + estimate_tokens(human_text) > budget
        and not (session.file_conversation_histories.get(file_path))
    ):
        await send_json(
            ws,
            "context_too_large",
            {
                "file_path": file_path,
                "question": human_text,
                "estimated_tokens": file_tokens,
                "budget_tokens": budget,
                "handoff_text": (f"Read {file_path} in this repo and answer this question about it:\n\n{human_text}"),
            },
        )
        return

    session.transcript.append({"role": "reviewer", "text": human_text})
    # index: -1 — a value no real hunk index ever has — is how the
    # frontend distinguishes an explore-mode turn from a hunk turn in the
    # shared transcript (see appendTurn/applyChatTabFilter in transcript.js).
    await send_json(
        ws, "human_turn", {"text": human_text, "index": -1, "total": len(session.hunks), "file_path": file_path}
    )

    history = session.file_conversation_histories.get(file_path, [])
    try:
        new_history, response_text = await run_llm(
            session,
            f"explore reply {file_path}",
            session.conversation.answer_about_file,
            history,
            file_path,
            content,
            human_text,
            from_voice,
        )
    except ConversationError as exc:
        log.info("Conversation agent unavailable: %s", exc)
        await send_json(ws, "service_status", {"llm": False})
        await send_error(ws, f"LLM call failed while responding: {exc}")
        return
    session.file_conversation_histories[file_path] = new_history
    await send_json(
        ws,
        "service_status",
        {
            "llm": True,
            "llm_input_tokens": session.conversation.total_input_tokens,
            "llm_output_tokens": session.conversation.total_output_tokens,
        },
    )

    session.transcript.append({"role": "presenter", "text": response_text})
    # No diff here — explore mode is about a whole file, not a hunk — so
    # _looks_like_diff_dump falls back to its lang/@@-header signals only.
    # Still catches the model pasting back a chunk of the file it was shown.
    explore_blocks, explore_spoken = sanitize_persona_reply(response_text, diff_context="")
    await send_json(
        ws,
        "reviewer_turn",
        {
            "text": response_text,
            "blocks": [block_to_payload(b) for b in explore_blocks],
            "spoken": explore_spoken,
            "index": -1,
            "total": len(session.hunks),
            "file_path": file_path,
        },
    )
    await try_speak(ws, session, explore_spoken)
