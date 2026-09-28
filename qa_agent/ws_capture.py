"""Reusable WebSocket frame-capture helper for qa_agent tests that need
to observe the app's own WS traffic (Stage 2's judges, in particular),
not just DOM state — e.g. the "narration" message's text, which never
shows up verbatim anywhere in the DOM the same way. Same
page.on("websocket", ...) pattern proven in this session's manual
verification scripts, just made reusable.
"""

from __future__ import annotations

import json
import time


class WsFrames:
    """Attach to a page *before* navigation (frames sent during the
    initial connect — "presenting", "service_status", etc. — would
    otherwise be missed) via `.attach(page)`, then use `.wait_for(...)`
    to retrieve a specific message type once it arrives."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def _on_frame_received(self, payload) -> None:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(data, dict):
            self.frames.append(data)

    def attach(self, page) -> None:
        def on_ws(ws):
            ws.on("framereceived", self._on_frame_received)

        page.on("websocket", on_ws)

    def wait_for(self, msg_type: str, page, timeout_ms: int = 15000) -> dict:
        """Polls for the most recent frame of msg_type. Playwright has no
        direct "wait for a websocket frame" primitive, so this polls via
        the page's own event loop (page.wait_for_timeout) rather than a
        plain time.sleep, so Playwright's other event processing (the
        frame listener itself) keeps running while we wait."""
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            matches = [f for f in self.frames if f.get("type") == msg_type]
            if matches:
                return matches[-1]["payload"]
            page.wait_for_timeout(100)
        raise TimeoutError(f"no {msg_type!r} WS frame received within {timeout_ms}ms")
