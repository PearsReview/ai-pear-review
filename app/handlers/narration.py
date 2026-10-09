"""A hunk's narration and the reviewer's replies about it: briefing, the
presenter turn, and the conversational reply."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging

from fastapi import WebSocket

from ..services.briefing_service import Briefing, ChangeContext
from ..services.conversation_service import ConversationError
from ..services.diff_service import Hunk
from ..services.editor_service import code_fence
from ..services.voice_service import VoiceServiceError
from ..utils.markdown_speech import block_to_payload, sanitize_persona_reply
from ..web import runtime
from ..web.context import (
    augment_with_marked_context,
    build_project_context,
    change_context,
    estimate_tokens,
    exceeds_budget,
    fit_history_to_budget,
    marked_lines_context,
    prompt_budget,
)
from ..web.progress import send_review_progress
from ..web.question_context import question_context
from ..web.runtime import BRIEFING, run_llm, send_error, send_json
from ..web.session import Session
from ..web.speech import try_speak
from .registry import handler

log = logging.getLogger("ai_pear_review")


async def cached_briefing_only(session: Session, hunk: Hunk) -> Briefing:
    """Returns the session or on-disk briefing for a hunk, never generating
    one.

    For a hunk too large to send, generating would ship the same oversized
    diff — exactly the timeout the briefing fallback exists to avoid."""
    briefing = session.briefings.get(session.index)
    if briefing is None:
        briefing = await asyncio.to_thread(BRIEFING.load_cached, hunk) or Briefing.unavailable()
        session.briefings[session.index] = briefing
    return briefing


async def load_change_context(session: Session, briefing: Briefing | None) -> ChangeContext | None:
    """Loads the change context for a briefing, off the event loop.

    Wraps change_context, which reads .context/ and other hunks' briefings
    from disk."""
    if briefing is None or not briefing.usable:
        return None
    return await asyncio.to_thread(change_context, session, briefing, BRIEFING.load_cached)


async def send_fallback_notice(
    ws: WebSocket,
    session: Session,
    hunk: Hunk,
    question: str | None,
    reason: str,
    budget: int | None,
    answered_from_briefing: bool,
) -> None:
    """Tells the client that something was answered without the diff in
    front of the model.

    Sends "context_too_large", the same message explore mode uses (see
    handle_explore_reply), with kind "hunk" and a reason: "too_large" when
    the diff cannot fit the window, "call_failed" when the normal call
    failed and the briefing was used instead. The payload carries a hand-off
    the reviewer can paste into a model able to read the whole change."""
    ask = question or "Why was this change made, and what does it do?"
    await send_json(
        ws,
        "context_too_large",
        {
            "kind": "hunk",
            "reason": reason,
            "file_path": hunk.file_path,
            "question": question,
            "estimated_tokens": estimate_tokens(hunk.diff_context),
            "budget_tokens": budget,
            "answered_from_briefing": answered_from_briefing,
            "handoff_text": (
                f"In this repo, look at the uncommitted change to {hunk.file_path} "
                f"(the hunk at {hunk.header}) and answer this about it:\n\n{ask}"
            ),
        },
    )


async def get_briefing(ws: WebSocket, session: Session, hunk: Hunk) -> Briefing:
    """Computes the current hunk's briefing on first view and caches it.

    Prev and Next then reuse it instead of regenerating, and a hunk the
    reviewer never opens costs nothing. BRIEFING.analyze_hunk checks its own
    on-disk cache first (see briefing_service.py); the per-session dict here
    is a second, faster layer for repeat views within one connection.

    A briefing that fails or is unavailable falls back to
    Briefing.unavailable() rather than blocking the presentation —
    present_hunk treats that the same as having no briefing at all (see
    conversation_service.py)."""
    cached = session.briefings.get(session.index)
    if cached is not None:
        return cached
    try:
        briefing = await run_llm(
            session, f"briefing hunk {session.index}", BRIEFING.analyze_hunk, hunk, session.conversation
        )
        await send_json(ws, "service_status", {"briefing": True})
    except ConversationError as exc:
        log.info("Briefing unavailable: %s", exc)
        briefing = Briefing.unavailable()
        await send_json(ws, "service_status", {"briefing": False})
    session.briefings[session.index] = briefing
    return briefing


async def _commit_narration(ws: WebSocket, session: Session, new_history: list[dict], text: str) -> None:
    # Committed together, synchronously, right after the only await that
    # could have been cancelled mid-flight. Mutating
    # session.conversation_histories from inside the background thread
    # instead would let a cancelled call's orphaned thread finish and
    # silently append a turn nobody saw, with no matching narrations entry
    # — see ConversationClient._send()'s docstring.
    session.conversation_histories[session.index] = new_history
    session.narrations[session.index] = text
    await send_json(
        ws,
        "service_status",
        {
            "llm": True,
            "llm_input_tokens": session.conversation.total_input_tokens,
            "llm_output_tokens": session.conversation.total_output_tokens,
        },
    )


async def _generate_narration(ws: WebSocket, session: Session, hunk: Hunk) -> tuple[str, str | None, int | None] | None:
    """A fresh narration for the current hunk, committed to session state.
    Returns (text, fallback_reason, budget): fallback_reason is None for a
    normal narration, "too_large" when the diff overflowed the window, or
    "call_failed" when the normal call failed and the briefing was used
    instead. None when nothing could be narrated (already reported).

    An oversized hunk is never sent whole: it is narrated from its cached
    briefing alone or, with no usable briefing, given a plain statement and
    no model call."""
    history = session.conversation_histories.get(session.index, [])
    # Only on the opening turn of a hunk: once it's in history, it
    # stays there for the rest of that hunk's conversation, and
    # repeating it every turn would spend budget re-saying what the
    # model can already see.
    project_context = build_project_context(session, hunk) if not history else None
    budget = prompt_budget(session)
    oversized = exceeds_budget(
        budget, session.conversation.prompts.persona_system, history, hunk.diff_context + (project_context or "")
    )

    if oversized:
        briefing = await cached_briefing_only(session, hunk)
        if not briefing.usable:
            text = (
                f"This hunk is too large for the local model to read (about "
                f"{estimate_tokens(hunk.diff_context)} tokens against a {budget}-token budget), "
                "and it has no briefing to narrate from."
            )
            session.narrations[session.index] = text
            return text, "too_large", budget
        return await _narrate_from_briefing(ws, session, hunk, briefing, "too_large", budget)

    briefing = await get_briefing(ws, session, hunk)
    context = await load_change_context(session, briefing)
    try:
        new_history, text = await run_llm(
            session,
            f"narrate hunk {session.index}",
            session.conversation.present_hunk,
            history,
            hunk,
            briefing,
            project_context,
            context,
        )
    except ConversationError as exc:
        log.info("Conversation agent unavailable: %s", exc)
        if briefing.usable:
            # One retry without the diff: on a slow CPU model a timed-out
            # narration is usually the prompt's size, and the briefing
            # prompt is a fraction of it.
            return await _narrate_from_briefing(ws, session, hunk, briefing, "call_failed", budget)
        await send_json(ws, "service_status", {"llm": False})
        await send_error(ws, f"LLM call failed while presenting hunk: {exc}")
        return None
    await _commit_narration(ws, session, new_history, text)
    return text, None, None


async def _narrate_from_briefing(
    ws: WebSocket, session: Session, hunk: Hunk, briefing: Briefing, reason: str, budget: int | None
) -> tuple[str, str | None, int | None] | None:
    context = await load_change_context(session, briefing)
    try:
        new_history, text = await run_llm(
            session,
            f"narrate hunk {session.index} from briefing ({reason})",
            session.conversation.present_from_briefing,
            hunk,
            briefing,
            context,
            reason,
        )
    except ConversationError as exc:
        log.info("Conversation agent unavailable: %s", exc)
        await send_json(ws, "service_status", {"llm": False})
        await send_error(ws, f"LLM call failed while presenting hunk: {exc}")
        return None
    await _commit_narration(ws, session, new_history, text)
    return text, reason, budget


def _briefing_as_text(briefing: Briefing, context: ChangeContext | None) -> str:
    """A usable briefing shown as plain text, for when there is no model to
    narrate from it at all (degraded mode)."""
    parts = []
    if briefing.summary:
        parts.append(f"What changed: {briefing.summary}")
    parts.append(f"Why: {briefing.intent}")
    if context is not None and context.theme_why:
        parts.append(f"Part of: {context.theme_title}. {context.theme_why}")
    return "\n".join(parts)


def _related_payload(context: ChangeContext | None) -> list[dict]:
    if context is None:
        return []
    return [
        {"index": r.index, "file_path": r.file_path, "relation": r.relation, "note": r.note} for r in context.related
    ]


async def present_current_hunk(ws: WebSocket, session: Session) -> None:
    """Shows whatever session.index and session.hunks resolve to, whether or
    not the review has ended.

    This function never redirects to the summary screen. If it did, every
    navigation control would break for the rest of a finished review: Prev,
    Next and a file-list click would each set session.index, get a real
    response, and receive the summary screen regardless of what was asked
    for — indistinguishable, from the reviewer's side, from the click doing
    nothing.

    So the places that do want the summary screen ask for it directly:
    _maybe_auto_end_review, the "end_review" handler, and the connect and
    refresh call sites. "show_summary" is how the reviewer gets back to it
    after browsing away.

    Automatic narration stops once the review has ended (see
    _narrates_automatically); explaining on request and replies still work. Only the code view keeps
    working, which matches how the app already behaves before a review
    starts."""
    hunk = session.current_hunk
    if hunk is None:
        await send_json(
            ws,
            "presenting",
            {"done": True, "index": session.index, "total": len(session.hunks)},
        )
        await send_review_progress(ws, session)
        return

    narration_available = session.conversation is not None
    # See narrate_current_hunk for when narration follows. Sent in
    # "presenting" so the client knows whether to show its "…thinking"
    # placeholder, and whether to offer "Explain this change".
    already_shown = session.index in session.narrations
    narrating = narration_available and (already_shown or _narrates_automatically(session))

    # Show the diff immediately — briefing (CLI cold-start) and the
    # conversation call are both real seconds, and there's no reason the
    # reviewer should stare at a blank pane while they run. The narration
    # text follows as a separate message once it's ready; cancel_current()
    # (called by the caller before starting a new hunk) already stops a
    # stale in-flight task from sending a late narration for a hunk the
    # reviewer has since moved past.
    await send_json(
        ws,
        "presenting",
        {
            "index": session.index,
            "total": len(session.hunks),
            "file_path": hunk.file_path,
            "header": hunk.header,
            "diff": hunk.diff_context,
            "full_lines": hunk.full_lines,
            "highlight_start": hunk.highlight_start,
            "highlight_end": hunk.highlight_end,
            "narration_available": narration_available,
            "review_started": session.review_started,
            # A real hunk (done: False) can be on screen after the review has
            # ended, because browsing and chat still work then. The client
            # needs this flag to keep Mark-as-reviewed, comments and Act Now
            # locked in that case, and to say the review is over — see
            # onPresenting's real-hunk branch in review-flow.js.
            "review_ended": session.review_ended,
            "narrated": already_shown,
            "narrating": narrating,
            "done": False,
        },
    )
    await send_review_progress(ws, session)
    await narrate_current_hunk(ws, session, hunk)


def _narrates_automatically(session: Session) -> bool:
    """Whether an unexplained hunk is explained just by moving to it."""
    return session.review_started and not session.review_ended and session.auto_narrate


async def narrate_current_hunk(ws: WebSocket, session: Session, hunk: Hunk, *, on_request: bool = False) -> None:
    """Sends the current hunk's narration: cached if it has one, otherwise
    generated — when moving to it narrates automatically, or when the
    reviewer asked (on_request, from "explain_hunk")."""
    narration_available = session.conversation is not None

    # True exactly when this hunk's text was already generated and cached
    # earlier in this connection (a brand-new Session is created per
    # websocket connection, so this can never be true on a hunk's very
    # first presentation, including right after a fresh reconnect). Gates
    # the two side effects below that must happen once per hunk, not once
    # per revisit: logging a transcript turn, and speaking the narration
    # aloud. Revisiting (Prev, jump_to_hunk, or landing back on a hunk via
    # Next) still needs the "presenting" message above and the "narration"
    # message below to refresh the view and clear the "…thinking"
    # placeholder — just not a duplicate log entry or a restarted reading.
    already_shown = session.index in session.narrations

    if (
        narration_available
        and not already_shown
        and not (_narrates_automatically(session) or (on_request and session.review_started))
    ):
        # Two reasons a FRESH narration call must not fire on its own: the
        # review hasn't started yet, or it has ended — never spend a model
        # call on a hunk nobody asked to be narrated. Asking (on_request,
        # the "Explain this change" button) still works after the end. A hunk that WAS already narrated before ending still
        # falls through past this (already_shown is True) to the cache-read
        # branch below, which sends no request anywhere — that's a pure
        # read of what was already generated, not new work, so it's fine
        # regardless of review_started/review_ended. The client shows a
        # "Start Review" CTA only in the not-started case, nothing extra in
        # the ended case (see onPresenting). A third reason: automatic
        # explanations are turned off (session.auto_narrate), and the
        # reviewer hasn't asked for this one — the client offers "Explain
        # this change" instead.
        return

    fallback: tuple[str, int | None] | None = None  # (reason, budget) when narrated without the diff
    if narration_available:
        if already_shown:
            # No service_status sent here: its absence is the signal that
            # this was a cache hit, not a fresh call.
            text = session.narrations[session.index]
        else:
            result = await _generate_narration(ws, session, hunk)
            if result is None:
                return
            text, reason, budget = result
            if reason is not None:
                fallback = (reason, budget)
    else:
        # No conversation agent for this connection (Ollama unreachable, or
        # ANTHROPIC_API_KEY not set if using that provider) — show the raw
        # diff with no persona narration, rather than refusing the whole
        # connection. No briefing call either: nothing would consume its
        # output right now. A prep-review briefing already on disk costs no
        # call, though, so its text is shown as-is. Cached in
        # session.narrations the same way real narration text is, so a
        # revisit doesn't re-log this either.
        text = session.narrations.get(session.index)
        if text is None:
            # The connection's own error names the provider and what to fix
            # (a missing key, an unreachable server); without one, point at
            # where the provider is chosen.
            reason = session.conversation_error or (
                "Check the model provider in Pear's settings (conversation.provider in config.yaml)."
            )
            text = f"Narration unavailable: {reason} Showing the raw diff only."
            briefing = await cached_briefing_only(session, hunk)
            if briefing.usable:
                context = await load_change_context(session, briefing)
                text += "\n\nFrom the prep-review briefing:\n" + _briefing_as_text(briefing, context)
            session.narrations[session.index] = text

    if not already_shown:
        session.transcript.append({"role": "presenter" if narration_available else "system", "text": text})

    # Sanitised only for real persona output — the degraded-mode
    # placeholder (narration_available False) is our own plain-English
    # message, not something that can carry a preamble or a diff dump, and
    # running it through a markdown parser meant for the model's replies
    # would be pointless at best. See sanitize_persona_reply's docstring:
    # `text` itself is sent unchanged below (llm_capture.py's response-text
    # join depends on that), only `blocks`/what's spoken are derived.
    if narration_available:
        blocks, spoken = sanitize_persona_reply(text, hunk.diff_context)
    else:
        blocks, spoken = [], text

    # Related-hunk links come from whatever briefing this hunk has by now;
    # computed on every present (not cached) so a revisit gets them too.
    related = _related_payload(await load_change_context(session, session.briefings.get(session.index)))
    await send_json(
        ws,
        "narration",
        {
            "text": text,
            "related": related,
            "blocks": [block_to_payload(b) for b in blocks],
            # What the turn's speaker button reads aloud ("speak_turn") —
            # the same sanitised text try_speak gets below.
            "spoken": spoken if narration_available else "",
            "narration_available": narration_available,
            # So the frontend can label which hunk this turn belongs to and
            # offer a way back to it — a transcript spanning many quickly-
            # visited hunks is otherwise an undifferentiated wall of text.
            "index": session.index,
            "total": len(session.hunks),
            "file_path": hunk.file_path,
        },
    )
    if fallback is not None:
        await send_fallback_notice(
            ws,
            session,
            hunk,
            None,
            fallback[0],
            fallback[1],
            answered_from_briefing=session.briefings[session.index].usable,
        )
    if narration_available and not already_shown:
        await try_speak(ws, session, spoken)


@handler("set_narration_prefs")
async def handle_set_narration_prefs(ws: WebSocket, session: Session, payload: dict) -> None:
    """Records whether hunks are explained automatically.

    Payload: {"auto_narrate": bool}. Like "set_voice_prefs": sent on every
    change, no reply. It applies from the next hunk moved to; the one on
    screen keeps what it has, and can always be explained on request."""
    if isinstance(payload.get("auto_narrate"), bool):
        session.auto_narrate = payload["auto_narrate"]


@handler("explain_hunk", cancels=True, background=True)
async def handle_explain_hunk(ws: WebSocket, session: Session, payload: dict) -> None:
    """Explains the current hunk on request — the toolbar's "Explain"
    button, shown when automatic explanations are off.

    Payload: {"index": int}, the hunk the button was pressed on. If the
    reviewer has moved on since, the request is for a hunk no longer on
    screen and is dropped. Replies with "narration", exactly as moving to
    the hunk would have."""
    hunk = session.current_hunk
    if hunk is None or payload.get("index") != session.index:
        return
    if not session.review_started:
        await send_error(ws, "Explanations are available once the review has started.")
        return
    if session.conversation is None:
        await send_error(ws, session.conversation_unavailable("explanations"))
        return
    await narrate_current_hunk(ws, session, hunk, on_request=True)


@handler("reply", cancels=True, background=True)
async def handle_reply(ws: WebSocket, session: Session, payload: dict) -> None:
    """Answers the reviewer's question about the current hunk.

    Payload: {"text": ...} or {"audio_base64": ...}, plus optional
    "marked_lines" (see augment_with_marked_context). Voice is transcribed
    here, the same way handle_request_change does it.

    Replies with "human_turn" as soon as the reviewer's words are known as
    text, then "reviewer_turn" once the model has answered — see
    _send_reviewer_turn."""
    # Not gated on review_ended: ending a review freezes its marks and
    # comments, not the conversation. Asking about a change after the
    # review is over is as useful as before it (see "reopen_review" for
    # getting the marks and comments back).
    hunk = session.current_hunk
    if hunk is None:
        await send_error(ws, "No hunk is currently being reviewed.")
        return

    if session.conversation is None:
        await send_error(ws, session.conversation_unavailable("replies"))
        return

    human_text = payload.get("text")
    audio_b64 = payload.get("audio_base64")
    from_voice = not human_text and bool(audio_b64)  # drives whether the persona is told this might be mis-transcribed

    if not human_text and audio_b64:
        try:
            audio_bytes = base64.b64decode(audio_b64)
            human_text = await asyncio.to_thread(runtime.STT.transcribe, audio_bytes)
            await send_json(ws, "service_status", {"stt": True})
        except VoiceServiceError as exc:
            log.info("STT unavailable: %s", exc)
            await send_json(ws, "service_status", {"stt": False})
            await send_error(ws, "Voice input is unavailable right now — please type your reply.")
            return
        except (binascii.Error, TypeError) as exc:
            # A malformed/truncated base64 string — e.g. a MediaRecorder
            # blob cut short because the browser tab was backgrounded
            # mid-recording — raises binascii.Error (bad padding/chars) or
            # TypeError (wrong argument type), neither of which is a
            # VoiceServiceError, so it needs its own handler rather than
            # propagating out of this inline (non-task) dispatch. Same
            # user-facing shape as the STT-unavailable branch above: the
            # reviewer just sees voice input didn't work this time, not a
            # stack trace.
            log.info("Malformed audio payload for reply: %s", exc)
            await send_json(ws, "service_status", {"stt": False})
            await send_error(ws, "Couldn't process the recorded audio — please try again.")
            return

    if not human_text:
        # Covers a truly empty payload and runtime.STT.transcribe() filtering out an
        # apparent silence-hallucination (see voice_service.py) — either
        # way, nothing real to reply to.
        await send_error(ws, "Didn't catch anything — please try again.")
        return

    session.transcript.append({"role": "reviewer", "text": human_text})
    # {"text", "index", "total", "file_path"}, sent as soon as the text is
    # known — before the (possibly much slower) LLM call below — so the
    # reviewer sees what was heard/typed right away instead of only once
    # the whole round trip finishes. Split out from "reviewer_turn", which
    # now carries the agent's response text alone.
    await send_json(
        ws,
        "human_turn",
        {"text": human_text, "index": session.index, "total": len(session.hunks), "file_path": hunk.file_path},
    )

    history = session.conversation_histories.get(session.index, [])
    question_text = augment_with_marked_context(human_text, payload.get("marked_lines"))

    # Facts this particular question calls for (tests, callers, line
    # history, a definition) — see app/web/question_context.py. Optional:
    # dropped first if the prompt doesn't fit.
    briefing = session.briefings.get(session.index)
    if briefing is None:
        # Read, not cached into session.briefings: narration may still want to
        # generate one for this hunk, and a cached "unavailable" would stop it.
        briefing = await asyncio.to_thread(BRIEFING.load_cached, hunk)
    looked_up = await asyncio.to_thread(
        question_context, session, hunk, human_text, history, bool(briefing and briefing.usable)
    )
    if looked_up is not None:
        log.info("reply hunk %s: question context from %s", session.index, ", ".join(looked_up.routes))

    budget = prompt_budget(session)
    system_prompt = session.conversation.prompts.persona_system
    candidates = [f"{looked_up.text}\n\n{question_text}"] if looked_up is not None else []
    candidates.append(question_text)
    for candidate in candidates:
        prompt_text = _with_diff_if_needed(hunk, history, candidate)
        fitted, dropped = fit_history_to_budget(history, prompt_text, system_prompt, budget)
        if not exceeds_budget(budget, system_prompt, fitted, prompt_text):
            break
    history = fitted
    if exceeds_budget(budget, system_prompt, history, prompt_text):
        has_excerpt = marked_lines_context(payload.get("marked_lines")) is not None
        await _reply_from_briefing(
            ws, session, hunk, human_text, question_text, from_voice, "too_large", budget, has_excerpt
        )
        return
    if dropped:
        # Say so rather than quietly shortening the model's memory: the
        # transcript still shows every turn, so without this the reviewer
        # has no way to know the model can no longer see the earlier ones.
        await send_json(
            ws,
            "notice",
            {
                "level": "info",
                "message": (
                    f"Trimmed {dropped} earlier turn(s) from the model's context to stay within "
                    f"the {session.conversation.num_ctx}-token window. They're still in your "
                    f"transcript — only what's sent to the model was shortened."
                ),
            },
        )

    try:
        new_history, response_text = await run_llm(
            session,
            f"reply hunk {session.index}",
            session.conversation.respond_to_reviewer,
            history,
            prompt_text,
            from_voice,
        )
    except ConversationError as exc:
        log.info("Conversation agent unavailable: %s", exc)
        if (await cached_briefing_only(session, hunk)).usable:
            # Same one retry without the diff as narration takes.
            await _reply_from_briefing(
                ws, session, hunk, human_text, question_text, from_voice, "call_failed", budget, has_excerpt=False
            )
            return
        await send_json(ws, "service_status", {"llm": False})
        await send_error(ws, f"LLM call failed while responding: {exc}")
        return
    # See present_current_hunk's matching comment — committed
    # synchronously, immediately after the only cancellable await, so a
    # cancelled/orphaned call's result is simply never assigned here.
    session.conversation_histories[session.index] = new_history
    spoken = await _send_reviewer_turn(ws, session, hunk, response_text)
    await try_speak(ws, session, spoken)


def _with_diff_if_needed(hunk: Hunk, history: list[dict], question_text: str) -> str:
    """Grounds a reply that would otherwise have no context at all.

    The diff only enters a hunk's history through its narration prompt.
    Without this, a question asked before narration lands — or in degraded
    mode, where narration never lands at all — reaches the model as nothing
    but the bare question, and comes back with a confidently invented
    answer.

    Prepending the hunk here fixes both cases at the cause, rather than
    gating the composer on a race the reviewer cannot see."""
    if history:
        return question_text
    fence = code_fence(hunk.diff_context)
    return f"File: {hunk.file_path}\n\nDiff hunk:\n{fence}\n{hunk.diff_context}\n{fence}\n\n{question_text}"


async def _reply_from_briefing(
    ws: WebSocket,
    session: Session,
    hunk: Hunk,
    human_text: str,
    question_text: str,
    from_voice: bool,
    reason: str,
    budget: int | None,
    has_excerpt: bool,
) -> None:
    """A reply without the diff, for a hunk too large for the window
    ("too_large") or after the normal reply call failed ("call_failed").
    Answers from what does fit: the cached briefing, its change context and
    any lines the reviewer selected. Always followed by the fallback notice
    and its hand-off, so an answer that isn't grounded in the code is never
    the last word.

    The fallback's history replaces the hunk's: it has no diff in it, so
    the next reply fits and continues normally from there."""
    briefing = await cached_briefing_only(session, hunk)
    context = await load_change_context(session, briefing)
    conversation = session.conversation
    fallback_prompt = conversation.briefing_only_prompt(hunk, briefing, context, question_text, reason)
    answered = False
    if (briefing.usable or has_excerpt) and not exceeds_budget(
        budget, conversation.prompts.persona_system, [], fallback_prompt
    ):
        try:
            new_history, response_text = await run_llm(
                session,
                f"reply hunk {session.index} from briefing ({reason})",
                conversation.answer_from_briefing,
                hunk,
                briefing,
                question_text,
                context,
                reason,
                from_voice,
            )
        except ConversationError as exc:
            log.info("Conversation agent unavailable: %s", exc)
            await send_json(ws, "service_status", {"llm": False})
            await send_error(ws, f"LLM call failed while responding: {exc}")
        else:
            session.conversation_histories[session.index] = new_history
            spoken = await _send_reviewer_turn(ws, session, hunk, response_text)
            answered = True
    await send_fallback_notice(ws, session, hunk, human_text, reason, budget, answered_from_briefing=answered)
    if answered:
        await try_speak(ws, session, spoken)


async def _send_reviewer_turn(ws: WebSocket, session: Session, hunk: Hunk, response_text: str) -> str:
    """Reports usage and delivers one reply turn; returns the spoken form
    for the caller to hand to try_speak once anything else is sent."""
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
    reply_blocks, reply_spoken = sanitize_persona_reply(response_text, hunk.diff_context)
    await send_json(
        ws,
        "reviewer_turn",
        {
            "text": response_text,
            "blocks": [block_to_payload(b) for b in reply_blocks],
            "spoken": reply_spoken,
            "index": session.index,
            "total": len(session.hunks),
            "file_path": hunk.file_path,
        },
    )
    return reply_spoken
