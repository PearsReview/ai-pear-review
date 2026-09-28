"""FastAPI + WebSocket backend for AI Pear Review.

This module owns the app object, the Origin check, the connect sequence
and the dispatch loop — nothing else. Every message is
{"type": ..., "payload": {...}}; the full contract, both directions, is
[wire-protocol.md](../docs/wire-protocol.md), and each message's reasoning
lives in the handler that implements it (app/handlers/, registered through
handlers/registry.py).

Three facts about the connect sequence below, because they are properties
of this file rather than of any one message:

- Every connection builds a brand-new Session, but session_store.py may
  seed it from disk: a review that was already started or ended, hunks
  marked reviewed, comments still queued. So a fresh socket is not
  necessarily a fresh review, and "review_comments_sync" is not
  necessarily empty.
- review_started and review_ended gate narration, replies and the review
  marks, never browsing. Stepping through hunks, opening any file and
  asking about it are all meant to work before a review starts and after
  it ends — see the individual handlers for which side each falls on.
- A conversation agent that fails to construct is not fatal. The
  connection continues in degraded mode (see the Session docstring): the
  reviewer still steps through hunks and reads the diff, without persona
  narration or replies.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .handlers import dispatch
from .handlers.comments import pending_comments_payload
from .handlers.narration import present_current_hunk
from .services.conversation_service import ConversationClient, ConversationError
from .services.diff_service import DiffError, get_review_hunks
from .services.session_store import load_persisted_state, reconcile_reviewed
from .web.config import BOUND_TO_LOOPBACK, CONFIG, STATIC_DIR, is_loopback_host
from .web.progress import send_review_progress, send_summary_screen
from .web.runtime import act_now_status, cancel_current, send_error, send_json
from .web.session import Session

log = logging.getLogger("ai_pear_review")

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


def _origin_is_trusted(ws: WebSocket) -> bool:
    """CSWSH guard: WebSocket connections are not covered by the same-
    origin policy the way fetch()/XHR are, so without this check any page
    open in any other browser tab — not just this app's own page — could
    open this same ws://.../ws URL and drive the full protocol (queue
    review comments, read arbitrary files via Act Now before the confirm
    step, enumerate definitions, ...).

    Compares the Origin header against the Host header the browser
    actually used to reach this server, rather than against a fixed
    configured value — this stays correct regardless of whether the
    server is bound to a loopback address or 0.0.0.0 (see
    is_loopback_host's warning in web/config.py), since Host always
    reflects what the browser put in the request, not what the server was
    configured with. A missing Origin is rejected too: real browsers always
    send one on a WebSocket handshake (confirmed by qa_agent's own
    Playwright-driven connections, which is why this is safe to require),
    so its absence means either a spoofed header or a client this protocol
    was never meant for.

    Origin == Host alone is not enough, because an attacker controls both
    on a DNS rebinding attack: evil.test resolves to a real address long
    enough for the browser to load the page, then re-resolves to 127.0.0.1,
    and the page's own requests carry Origin: http://evil.test:8765 and
    Host: evil.test:8765 — equal, and pointing here. So when this server is
    bound to loopback, the name the browser used must be a loopback name
    too; "evil.test" is not, however it resolves. A deliberately
    network-bound server (see BOUND_TO_LOOPBACK, already warned about at
    startup) can't apply that rule — a real remote reviewer's Host is
    someone else's hostname — so it keeps the equality check alone."""
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    if not origin or not host:
        return False
    try:
        origin_netloc = urllib.parse.urlsplit(origin).netloc
    except ValueError:
        return False
    if origin_netloc.lower() != host.lower():
        return False
    return not BOUND_TO_LOOPBACK or _host_is_loopback(host)


def _host_is_loopback(host_header: str) -> bool:
    """Whether a Host header names this machine. Splitting it as a URL
    authority is what strips the port and the brackets around an IPv6
    address ("[::1]:8765" -> "::1"), both of which is_loopback_host would
    otherwise reject."""
    try:
        hostname = urllib.parse.urlsplit(f"//{host_header}").hostname
    except ValueError:
        return False
    return bool(hostname) and is_loopback_host(hostname)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    if not _origin_is_trusted(ws):
        # Reject before accept() — Starlette turns this into an HTTP 403
        # at the handshake rather than a normal WS close frame, so a
        # disallowed page's connection attempt fails outright instead of
        # briefly opening and then closing.
        await ws.close(code=1008)
        return

    await ws.accept()

    repo_path = CONFIG["server"].get("repo_path", ".")
    try:
        # to_thread, not a direct call — this runs a `git diff` subprocess,
        # and unlike the identical call in handle_refresh_diff (already
        # wrapped), this one used to run straight on the event loop. Under
        # normal single-tab use a git call is imperceptibly fast, but it
        # blocks *every* connection on this single-process server for its
        # duration — the qa_agent suite's rapid reconnects, combined with
        # its own concurrent git operations on the same scratch repo,
        # turned one slow/contended git call into a total server freeze
        # for all clients. Caught by qa_agent, not by any human tester,
        # since a human only ever opens one tab at a time.
        hunks = await asyncio.to_thread(get_review_hunks, repo_path)
    except DiffError as exc:
        await send_error(ws, f"Could not read the diff: {exc}")
        await ws.close()
        return

    conversation: ConversationClient | None
    try:
        # CONFIG["debug"] is the test-only LLM-call capture block (absent
        # and inert in any normal run — see config.yaml and
        # ConversationClient._capture_call).
        # Off the event loop: construction asks the model server what the
        # configured model is (see ChatProvider.prepare).
        conversation = await asyncio.to_thread(ConversationClient, CONFIG["conversation"], CONFIG.get("debug"))
    except ConversationError as exc:
        # Not fatal to the connection — degraded mode (see Session docstring):
        # the reviewer can still step through hunks and see the raw diff,
        # just without persona narration or replies.
        log.info("Conversation agent unavailable for this connection: %s", exc)
        conversation = None

    session = Session(hunks, conversation, repo_path)
    # Read from the URL, not a message: the first hunk is presented below
    # before any message from the client could be handled, and it must not
    # be narrated if the reviewer turned automatic explanations off.
    session.auto_narrate = ws.query_params.get("auto_narrate") != "0"
    # Resume silently if there's persisted state for this repo (see
    # session_store.py). reconcile_reviewed re-keys by hunk content
    # (stable_hunk_key), not position, so this is safe even if the diff has
    # shifted since the state was last saved.
    persisted = load_persisted_state(repo_path)
    if persisted is not None:
        session.review_started = persisted.review_started
        session.review_ended = persisted.review_ended
        session.reviewed = reconcile_reviewed(hunks, persisted.reviewed_hunk_keys)
        session.pending_review_comments = persisted.pending_review_comments
        session.next_comment_id = persisted.next_comment_id
    await send_json(
        ws,
        "service_status",
        {
            "stt": True,
            "tts": True,
            "llm": conversation is not None,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            # Optimistic like stt/tts above — a cached briefing can work
            # even in degraded mode (no live connection), so there's no
            # single startup fact that determines this; the real value
            # corrects itself via service_status on first actual use
            # (see get_briefing).
            "briefing": True,
            "act_now": act_now_status(),
        },
    )
    await send_review_progress(ws, session)
    # Sent whether or not anything is queued, so the client has one
    # hydration path for "what is queued right now" rather than assuming
    # empty-on-connect and relying solely on the incremental
    # review_comment_queued/_updated/_removed events.
    await send_json(ws, "review_comments_sync", {"comments": pending_comments_payload(session)})

    if not hunks:
        await send_error(ws, "No changes found to review. Make some changes to tracked files and reconnect.")
    elif session.review_ended:
        # A fresh connection into an already-ended review (persisted from
        # a previous connection, or a full app restart — see
        # load_persisted_state above) opens on the wrap-up screen, not
        # hunk 0 — present_current_hunk no longer redirects there on its
        # own (see its docstring), so this has to ask for it explicitly.
        # index is still 0 here, a sane starting point for whatever the
        # reviewer browses to next via "show_summary" or a file click.
        session.index = 0
        await send_summary_screen(ws, session)
    else:
        # Load the first hunk automatically — a fresh connection otherwise
        # shows an empty code pane with no cue that "Next" needs a click.
        session.index = 0
        session.current_task = asyncio.create_task(present_current_hunk(ws, session))

    try:
        while True:
            msg = await ws.receive_json()
            await dispatch(ws, session, msg.get("type"), msg.get("payload", {}))
    except WebSocketDisconnect:
        cancel_current(session)
