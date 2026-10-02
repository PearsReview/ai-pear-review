"""The review-progress and summary-screen messages — sent from several
handler modules, so they live below all of them."""

from __future__ import annotations

from fastapi import WebSocket

from ..services.review_handoff import latest_review_plan
from .runtime import send_json
from .session import Session


async def send_review_progress(ws: WebSocket, session: Session) -> None:
    """Sends the reviewed counts and the edited-file list to the client.

    Reviewed status is explicit, never inferred from a hunk having been
    presented: a reviewer might open a hunk, discuss it, and mark it only
    once satisfied. Sent after every hunk transition and every toggle, so
    the UI never has to guess counts from the presenting payload alone.

    The file list is one entry per distinct file_path in session.hunks, in
    first-appearance order, so the UI can show the scope of the whole
    review rather than just the hunk on screen. Cheap to carry here, since
    reviewed_count per file needs recomputing on the same events as the
    overall count anyway."""
    files: dict[str, dict] = {}
    for i, hunk in enumerate(session.hunks):
        entry = files.setdefault(
            hunk.file_path,
            {"file_path": hunk.file_path, "hunk_count": 0, "reviewed_count": 0, "first_index": i},
        )
        entry["hunk_count"] += 1
        if i in session.reviewed:
            entry["reviewed_count"] += 1

    await send_json(
        ws,
        "review_progress",
        {
            "reviewed_count": len(session.reviewed),
            "total": len(session.hunks),
            "current_reviewed": session.index in session.reviewed,
            "files": list(files.values()),
            # Lets the file sidebar tell "not started yet" apart from
            # "started, nothing reviewed yet" (both look like 0 reviewed)
            # without depending on message order against "presenting"'s own
            # review_started — this message carries its own copy instead.
            "review_started": session.review_started,
            # Same idea, same reason — the client needs to know the review
            # has ended even while review_progress arrives independently of
            # a "presenting" payload (e.g. after toggling a comment), now
            # that a real hunk can be on screen post-end (see
            # present_current_hunk).
            "review_ended": session.review_ended,
        },
    )


async def send_summary_screen(ws: WebSocket, session: Session) -> None:
    """The review-ended screen — takes over the code pane in place of
    whatever hunk was on screen, reachable from *any* hunk (every caller
    of present_current_hunk routes here first once session.review_ended is
    set, not just the old "stepped past the last hunk" case below).
    ended_early distinguishes "End Review was pressed before finishing"
    from "the last hunk got marked reviewed and this ended automatically"
    purely for the client's wording — otherwise both are the same state."""
    await send_json(
        ws,
        "presenting",
        {
            "done": True,
            "ended": True,
            "index": session.index,
            "total": len(session.hunks),
            "reviewed_count": len(session.reviewed),
            "pending_comment_count": len(session.pending_review_comments),
            "ended_early": len(session.reviewed) < len(session.hunks),
            # The newest hand-off plan on disk, for the summary's "View
            # review plan" button — None until a review has been finished.
            "review_plan": latest_review_plan(session.repo_path),
        },
    )
    await send_review_progress(ws, session)
