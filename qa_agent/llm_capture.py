"""Reads the app's test-only LLM-call captures (see config.yaml's `debug:`
block and ConversationClient._capture_call) so a finding can record exactly
what the model was given, not just what it answered.

Still filesystem-only, so the suite's isolation rule holds: nothing here
imports app.*, it just reads JSON the running app wrote. The app writes
those files into its own working directory, which under test is the
throwaway copy in pytest's tmp dir (conftest.py's app_copy_dir), so they
are discarded with the session — the point of the capture being test-only
in the first place.

Every function here degrades to "no capture available" rather than raising:
capture is opt-in, the app writes best-effort, and a missing record must
never turn a real judged finding into an error.
"""

from __future__ import annotations

import json
from pathlib import Path


def read_captures(capture_dir: Path) -> list[dict]:
    """Every capture written so far, oldest first.

    Ordered by file mtime rather than filename: names are
    `{connection}_{seq}.json`, so sorting them lexically would group by
    connection instead of by time, and the app can genuinely have two
    connections writing at once (each WebSocket builds its own
    ConversationClient with its own sequence). mtime is the only ordering
    that's true across connections.

    A file that fails to parse is skipped, not fatal — the app writes
    these best-effort while a test may be reading mid-run, so a torn write
    is a real possibility and not worth failing a judged test over."""
    if not capture_dir.exists():
        return []
    captures = []
    for path in sorted(capture_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        try:
            captures.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return captures


def find_capture_for_response(capture_dir: Path, response_text: str) -> dict | None:
    """The capture whose response is this exact text — i.e. the call that
    produced the reply/narration a test just read off a WS frame.

    Matching on the response rather than on a correlation id is deliberate:
    the app's own `llm[N]` call-id never reaches the client, but the
    response text appears verbatim in both places (the capture file and the
    "narration"/"reviewer_turn" payload), so it joins the two sides without
    needing anything threaded through the app's call sites.

    Searches newest-first, so when the same answer legitimately appears
    twice (a revisit replaying cached narration, say) the most recent call
    wins. Returns None when capture is switched off or nothing matches —
    callers attach the result as optional provenance and carry on."""
    if not response_text:
        return None
    stripped = response_text.strip()
    for capture in reversed(read_captures(capture_dir)):
        if capture.get("response", "").strip() == stripped:
            return capture
    return None
