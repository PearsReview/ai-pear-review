"""Process-wide clients and the plumbing every handler shares: the BRIEFING/
STT/TTS singletons, run_llm (the one choke point for model calls), the send
helpers and cancel_current.

STT and TTS are rebound by handle_set_settings (app/handlers/settings.py),
so read them as `runtime.STT` / `runtime.TTS` at the call site. A
`from .runtime import TTS` binds the client that existed at import time and
silently ignores a settings change."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import threading
import time

from fastapi import WebSocket

from ..providers.base import DEFAULT_PROVIDER
from ..services.briefing_service import BriefingClient
from ..services.conversation_service import check_conversation_available
from ..services.errors import ServiceError
from ..services.harness_service import agent_label, harness_status
from ..services.voice_service import STTClient, TTSClient
from .config import CONFIG
from .session import Session

log = logging.getLogger("ai_pear_review")

BRIEFING = BriefingClient(CONFIG["briefing"], CONFIG["server"].get("repo_path", "."))
STT = STTClient(CONFIG["stt"])
TTS = TTSClient(CONFIG["tts"])

# The TTS knobs this file needs, read once here rather than at each call
# site. Looking them up inline in several places is how mime_type ended up
# with a code default ("audio/mpeg") contradicting config.yaml's declared
# value ("audio/wav").
TTS_MIME_TYPE = CONFIG["tts"].get("mime_type", "audio/wav")
# The most this endpoint is sent in one request, honoured by every TTS path
# (see config.yaml's comment). Either may be None/0 for "no limit of that
# kind"; anything over budget is split into consecutive clips.
TTS_MAX_CHARS = CONFIG["tts"].get("max_chars", 800)
TTS_MAX_WORDS = CONFIG["tts"].get("max_words")

# Cheap startup probe so the UI's initial status bar isn't a lie. Real
# calls are attempted regardless and report their own pass or fail through
# service_status. Briefing generation shares this connection (see
# briefing_service.py), so there is no separate CLI probe to run — the app
# never calls the CLI itself.
CONVERSATION_AVAILABLE_AT_STARTUP = check_conversation_available(CONFIG["conversation"])
if not CONVERSATION_AVAILABLE_AT_STARTUP:
    # Detail (which URL, which env var) is preflight.py's job; this just
    # names the provider so the log line is never wrong about which one.
    log.warning(
        "conversation provider %r is not available at startup — the conversation agent "
        "will fail until this is fixed (the startup preflight report says why)",
        CONFIG["conversation"].get("provider", DEFAULT_PROVIDER),
    )


_llm_call_seq = itertools.count(1)


async def run_llm(session: Session, label: str, fn, *args):
    """Runs one blocking LLM call in a worker thread, with real
    cancellation and a log line at each end.

    Every LLM call in this file goes through here; nothing else should call
    asyncio.to_thread on something that talks to a model.

    Cancellation: to_thread cancellation abandons the *await*, it does not
    stop the worker thread. Arming a token before dispatch and setting it
    when the await is cancelled gives the provider client somewhere to
    notice and drop its connection, which is what actually stops generation
    and frees the slot in Ollama's serialized per-model queue (see
    ConversationClient.arm_cancel and OllamaProvider.complete). Without it
    an abandoned call — the reviewer hits Next, or the browser closes —
    runs to completion and the next real request queues behind it with no
    error and no log. That is the intermittent Act Now hang.

    Logging: that hang presents as total silence. A stall that is really
    "one request waiting behind three orphans" is indistinguishable from
    "nothing was asked of the server". A start/finish pair with a duration
    and an outcome makes the difference visible, and the cancelled-after
    time is the direct evidence that an abandoned call stopped promptly
    rather than running on."""
    cancel = session.conversation.arm_cancel()
    call_id = next(_llm_call_seq)
    started = time.monotonic()
    log.info("llm[%d] %s start", call_id, label)
    try:
        result = await asyncio.to_thread(fn, *args)
    except asyncio.CancelledError:
        cancel.set()
        log.info("llm[%d] %s cancelled after %.1fs", call_id, label, time.monotonic() - started)
        raise
    except ServiceError as exc:
        # Caught via the shared base so no service's failure goes unlogged —
        # the silence is the exact thing this helper exists to stop.
        log.info("llm[%d] %s failed after %.1fs: %s", call_id, label, time.monotonic() - started, exc)
        raise
    log.info("llm[%d] %s ok in %.1fs", call_id, label, time.monotonic() - started)
    return result


async def run_agent(label: str, fn, *args):
    """Runs one coding-agent turn in a worker thread — run_llm's counterpart
    for harness_service.py.

    Needs no ConversationClient. fn receives a fresh cancel token as its
    last argument, set when this await is cancelled, so the agent process is
    told to stop and reaped rather than left running. Same reasoning as
    run_llm."""
    cancel = threading.Event()
    call_id = next(_llm_call_seq)
    started = time.monotonic()
    log.info("agent[%d] %s start", call_id, label)
    try:
        result = await asyncio.to_thread(fn, *args, cancel)
    except asyncio.CancelledError:
        cancel.set()
        log.info("agent[%d] %s cancelled after %.1fs", call_id, label, time.monotonic() - started)
        raise
    except ServiceError as exc:
        log.info("agent[%d] %s failed after %.1fs: %s", call_id, label, time.monotonic() - started, exc)
        raise
    log.info("agent[%d] %s ok in %.1fs", call_id, label, time.monotonic() - started)
    return result


def act_now_status() -> dict:
    """Reports whether Act Now is usable, and the sentence its tooltip
    shows either way.

    This is service_status's "act_now" field. Read live rather than at
    startup, because the settings panel can switch agents mid-session."""
    status = harness_status(CONFIG["harness"])
    return {"available": status.available, "detail": status.detail, "agent": agent_label(CONFIG["harness"])}


async def send_json(ws: WebSocket, msg_type: str, payload: dict) -> None:
    await ws.send_json({"type": msg_type, "payload": payload})


async def send_error(ws: WebSocket, message: str, source: str | None = None) -> None:
    """`source` names the action that failed (e.g. "speak_file"), for a
    client that has to end that action's own state on the error rather
    than guess from the message wording."""
    payload = {"message": message}
    if source:
        payload["source"] = source
    await send_json(ws, "error", payload)


async def send_agent_stopped(ws: WebSocket, kind: str, message: str) -> None:
    """Tells the client an agent run (Look deeper, Act Now, a refine) was
    cancelled before it answered, so its spinner or locked bar is released.

    Not "error": the cancel usually comes from the reviewer's next action,
    whose own "…thinking" bubble showError would wipe. Suppressed failures:
    a cancel caused by the browser disconnecting has no socket to send to,
    and the caller must still re-raise the CancelledError."""
    with contextlib.suppress(Exception):
        await send_json(ws, "agent_stopped", {"kind": kind, "message": message})


def cancel_current(session: Session) -> None:
    if session.current_task is not None and not session.current_task.done():
        session.current_task.cancel()
    session.current_task = None
