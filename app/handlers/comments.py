"""Queued review comments and the Finish Review hand-off plan.

No LLM call happens anywhere in this module.

Comments accumulate rather than being sent one at a time. "request_change"
adds one to session.pending_review_comments, across however many files and
hunks the reviewer visits, and "finish_review" hands the whole batch off at
once, the way submitting a GitHub pull-request review does.

Each comment records a reference to its code, not a copy of whole files:
the diff lines it covers, a few lines either side, the hunk header and line
numbers (see app/services/review_handoff.py). Finishing turns the queue
into a structured review in memory and renders it as one markdown plan,
.review/review_<timestamp>.md (gitignored): a checklist with instructions
for the reviewer's coding agent. On request it also saves the plan as an
agent skill, .claude/skills/apply-review/SKILL.md, which Claude Code and
Cline both discover. It then clears the queue and replies
"review_finished" ({"plan_path", "plan_file", "as_skill", "skill_written",
"skill_note", "instruction_line", "comment_count"}). "plan_file" is the
plan's repo-relative path, which the client opens in its markdown preview;
"instruction_line" is what to give the reviewer's agent: "/apply-review"
when the skill was written, otherwise "Read .review/review_....md and
follow its instructions". "skill_note" says why a requested skill wasn't
written. Nothing is copied to the clipboard unasked: the client offers
copy buttons instead.

This module never invokes a coding agent itself: a human triggers the
apply step. (Act Now, in act_now.py, is the one path that edits files from
inside the app.)"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from datetime import datetime
from pathlib import Path

from fastapi import WebSocket

from ..services.diff_service import get_head_sha
from ..services.review_handoff import (
    SKILL_NAME,
    build_review,
    comment_location,
    render_plan,
    render_skill,
    write_skill,
)
from ..services.session_store import save_persisted_state
from ..services.voice_service import VoiceServiceError
from ..web import runtime
from ..web.context import line_context
from ..web.runtime import send_error, send_json
from ..web.session import Session
from .registry import handler

log = logging.getLogger("ai_pear_review")
# Triage tags stored on each queued comment; the hand-off plan orders and
# groups by them (see review_handoff.render_plan). A missing or
# unrecognised value falls back to DEFAULT_COMMENT_SEVERITY rather than
# rejecting the comment: never fail an interactive action over a cosmetic
# field.
#
# No "question" tag, unlike some review tools. A question tag only earns its
# place when something later reads the thread and answers it, and nothing
# here ever would — it would be a label that never gets a reply.
COMMENT_SEVERITIES = ("must-fix", "suggestion", "nit")
DEFAULT_COMMENT_SEVERITY = "suggestion"


def pending_comments_payload(session: Session) -> list[dict]:
    """Puts the whole queue into the shape the client expects.

    The same fields as one "review_comment_queued" payload, without
    pending_count — the length of this list already says that. Used for the
    "review_comments_sync" message sent on connect."""
    return [
        {
            "id": c["id"],
            "file_path": c["file_path"],
            "where": c["where"],
            "instruction": c["instruction"],
            # .get, not [] — comments persisted by a version of this app
            # before severity tags existed won't have the key.
            "severity": c.get("severity", DEFAULT_COMMENT_SEVERITY),
            "anchor": c["anchor"],
        }
        for c in session.pending_review_comments
    ]


@handler("request_change", cancels=True, background=True)
async def handle_request_change(ws: WebSocket, session: Session, payload: dict) -> None:
    """Queues one review comment. Nothing is sent anywhere until
    "finish_review".

    Payload: {"text": ...} or {"audio_base64": ...}, plus "marked_lines"
    identifying the lines being commented on and an optional "severity" from
    COMMENT_SEVERITIES. Sent from the inline comment box (see
    submitComposerText and submitComposerAudio in static/js/interactions.js).

    No model is involved: the instruction is stored exactly as given, which
    is why this path structurally cannot invent an action or argue back the
    way a conversational reply could.

    Nothing checks a voice transcription before it is queued, since there is
    no model here to notice a mismatch. The correction route is the edit and
    remove buttons on the resulting comment, not a confirmation step before
    queuing."""
    if not session.review_started or session.review_ended:
        # Comments are a review action like marking a hunk reviewed —
        # nothing to queue before the review has started, and nothing new
        # to add once it's ended (see Session docstring) until it's
        # reopened. The "+" button is already hidden client-side in both
        # states (see .comments-locked in style.css); this is
        # defense-in-depth against a crafted/stale request, not the
        # primary gate.
        await send_error(
            ws,
            "The review has ended — reopen it to add comments."
            if session.review_ended
            else "Start the review before adding comments.",
        )
        return
    hunk = session.current_hunk
    marked_lines = payload.get("marked_lines")
    if marked_lines:
        # Anchor on a real hunk for the marked file if one's loaded, purely
        # so "hunk is None" below still catches a truly empty session —
        # line_context uses marked_lines' own file_path regardless.
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
            # Same malformed-base64 case as handle_reply's identical guard
            # above (see that comment) — a truncated MediaRecorder blob
            # raises binascii.Error/TypeError, not VoiceServiceError, and
            # this handler is dispatched via create_task, so an uncaught
            # exception here would die silently with no error frame sent.
            log.info("Malformed audio payload for request_change: %s", exc)
            await send_json(ws, "service_status", {"stt": False})
            await send_error(ws, "Couldn't process the recorded audio — please try again.")
            return

    if not instruction:
        await send_error(ws, "Didn't catch anything — please try again.")
        return

    file_path, where, snippet, anchor = line_context(hunk, marked_lines)
    severity = payload.get("severity")
    if severity not in COMMENT_SEVERITIES:
        severity = DEFAULT_COMMENT_SEVERITY
    comment_id = session.next_comment_id
    session.next_comment_id += 1
    session.pending_review_comments.append(
        {
            "id": comment_id,
            "file_path": file_path,
            "where": where,
            "snippet": snippet,
            "instruction": instruction,
            "severity": severity,
            "anchor": anchor,
            **comment_location(session.hunks, hunk, marked_lines),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    save_persisted_state(session.repo_path, session)
    await send_json(
        ws,
        "review_comment_queued",
        {
            "id": comment_id,
            "file_path": file_path,
            "where": where,
            "instruction": instruction,
            "severity": severity,
            "anchor": anchor,
            "pending_count": len(session.pending_review_comments),
        },
    )


@handler("edit_change_request")
async def handle_edit_change_request(ws: WebSocket, session: Session, payload: dict) -> None:
    """Rewrites a queued comment's instruction, and its severity if one is
    given.

    Payload: {"id": int, "instruction": str, "severity": str optional}, sent
    from the edit button on a queued comment (see startEditingChangeRequest
    in static/js/code-view.js). An unrecognised severity leaves the existing one
    alone.

    Nothing has left this machine yet — that only happens at
    "finish_review" — so editing is a plain in-place change with nothing to
    rebuild."""
    comment_id = payload.get("id")
    instruction = (payload.get("instruction") or "").strip()
    severity = payload.get("severity")

    comment = next((c for c in session.pending_review_comments if c["id"] == comment_id), None)
    if comment is None:
        await send_error(ws, "That comment is no longer in the review queue.")
        return
    if not instruction:
        await send_error(ws, "Can't set an empty comment.")
        return

    comment["instruction"] = instruction
    if severity in COMMENT_SEVERITIES:
        comment["severity"] = severity
    save_persisted_state(session.repo_path, session)
    await send_json(
        ws,
        "review_comment_updated",
        {"id": comment_id, "instruction": instruction, "severity": comment.get("severity", DEFAULT_COMMENT_SEVERITY)},
    )


@handler("remove_review_comment")
async def handle_remove_review_comment(ws: WebSocket, session: Session, payload: dict) -> None:
    """Deletes a queued comment outright.

    Payload: {"id": int}. The same idea as deleting a pending comment from a
    GitHub review before submitting it."""
    comment_id = payload.get("id")
    before = len(session.pending_review_comments)
    session.pending_review_comments = [c for c in session.pending_review_comments if c["id"] != comment_id]
    if len(session.pending_review_comments) == before:
        await send_error(ws, "That comment is no longer in the review queue.")
        return
    save_persisted_state(session.repo_path, session)
    await send_json(
        ws, "review_comment_removed", {"id": comment_id, "pending_count": len(session.pending_review_comments)}
    )


def _new_plan_path(repo_path: str, stamp: str) -> Path:
    """.review/review_<stamp>.md, suffixed if a review already finished
    within the same second."""
    base = Path(repo_path) / ".review"
    base.mkdir(parents=True, exist_ok=True)
    candidate, n = base / f"review_{stamp}.md", 2
    while candidate.exists():
        candidate, n = base / f"review_{stamp}_{n}.md", n + 1
    return candidate


@handler("finish_review", writes=True)
async def handle_finish_review(ws: WebSocket, session: Session, payload: dict) -> None:
    """Writes the plan from the whole queue and hands it off (see the module
    docstring).

    Payload: {"note": "...", "as_skill": bool}, both optional. "note" is an
    overall review note not tied to any one line; "as_skill" also saves the
    plan as the apply-review agent skill (review_handoff.write_skill).

    Callable at any point pending_review_comments is non-empty; a single
    queued comment finished right away is just today's quick one-off,
    served by the same mechanism as a larger multi-file batch."""
    if not session.pending_review_comments:
        await send_error(ws, "No pending review comments to finish.")
        return

    overall_note = (payload.get("note") or "").strip()
    repo_path = session.repo_path
    now = datetime.now()
    base_commit = await asyncio.to_thread(get_head_sha, repo_path)
    review = await asyncio.to_thread(
        build_review,
        session.pending_review_comments,
        overall_note,
        repo_path,
        base_commit,
        now.isoformat(timespec="seconds"),
    )
    plan = render_plan(review)

    try:
        plan_path = _new_plan_path(repo_path, now.strftime("%Y%m%d_%H%M%S"))
        plan_path.write_text(plan, encoding="utf-8")
    except OSError as exc:
        await send_error(ws, f"Failed to save the review: {exc}")
        return

    as_skill = payload.get("as_skill") is True
    skill_note = None
    if as_skill:
        skill_note = await asyncio.to_thread(write_skill, repo_path, render_skill(plan, review["finished_at"]))

    finished_count = len(session.pending_review_comments)
    session.pending_review_comments = []
    # Persist the now-cleared queue — otherwise a restart right after
    # finishing would resurrect comments that were just handed off.
    save_persisted_state(session.repo_path, session)

    # Relative, so the pasted line works in an agent session opened at the
    # repo root whatever path this app was given.
    relative_plan = plan_path.relative_to(Path(repo_path)).as_posix()
    skill_written = as_skill and skill_note is None
    await send_json(
        ws,
        "review_finished",
        {
            "plan_path": plan_path.resolve().as_posix(),
            # What the client opens in the markdown preview (open_md_preview).
            "plan_file": relative_plan,
            "as_skill": as_skill,
            "skill_written": skill_written,
            "skill_note": skill_note,
            "instruction_line": (
                f"/{SKILL_NAME}" if skill_written else f"Read {relative_plan} and follow its instructions"
            ),
            "comment_count": finished_count,
        },
    )
