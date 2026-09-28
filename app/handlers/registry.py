"""The message-type -> handler table websocket_endpoint dispatches through.

Every registered handler takes (ws, session, payload) and declares two
things:

- `cancels` — call cancel_current first.
- `background` — run as session.current_task instead of awaiting inline.

`background` wraps the whole handler, so a handler that must change session
state synchronously before its task starts (next/prev: an index bump that a
second quick click has to see) stays inline and creates the task itself.

Preconditions — the review_started/review_ended guards — stay inside the
handlers rather than being declared here, because they don't fail alike:
some send an error, some quietly do nothing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import WebSocket

from ..web.runtime import cancel_current, send_error
from ..web.session import Session

HandlerFn = Callable[[WebSocket, Session, dict], Awaitable[None]]


@dataclass(frozen=True)
class HandlerSpec:
    fn: HandlerFn
    cancels: bool
    background: bool


HANDLERS: dict[str, HandlerSpec] = {}


def handler(msg_type: str, *, cancels: bool = False, background: bool = False) -> Callable[[HandlerFn], HandlerFn]:
    def register(fn: HandlerFn) -> HandlerFn:
        if msg_type in HANDLERS:
            raise ValueError(f"Two handlers registered for {msg_type!r}")
        HANDLERS[msg_type] = HandlerSpec(fn, cancels, background)
        return fn

    return register


async def dispatch(ws: WebSocket, session: Session, msg_type: object, payload: dict) -> None:
    # isinstance first: msg_type comes straight from client JSON, and an
    # unhashable value (a list) would raise out of dict.get and drop the
    # connection instead of getting the "unknown type" error.
    spec = HANDLERS.get(msg_type) if isinstance(msg_type, str) else None
    if spec is None:
        await send_error(ws, f"Unknown message type: {msg_type}")
        return
    if spec.cancels:
        cancel_current(session)
    if spec.background:
        session.current_task = asyncio.create_task(spec.fn(ws, session, payload))
    else:
        await spec.fn(ws, session, payload)
