"""Moving through a review: navigation, reviewed marks, starting/ending a
review, and re-reading the diff."""

from __future__ import annotations

import asyncio

from fastapi import WebSocket

from ..services.briefing_service import prune_briefing_cache
from ..services.diff_service import DiffError, get_review_hunks, stable_hunk_key
from ..services.session_store import reconcile_reviewed, save_persisted_state
from ..web.config import CONFIG
from ..web.progress import send_review_progress, send_summary_screen
from ..web.runtime import cancel_current, send_error, send_json
from ..web.session import Session
from .narration import present_current_hunk
from .registry import handler
from .settings import context_status

# What to call each prep file in the drift warning. The settings panel has
# its own copy of these labels (CONTEXT_FILE_LABELS in settings.js) because it
# renders the full status list; this only ever names the stale ones.
_PREP_LABELS = {
    "project_overview": "project overview",
    "call_map": "call map",
    "changeset": "change themes",
}


async def _maybe_auto_end_review(ws: WebSocket, session: Session) -> bool:
    """Ends the review once every hunk is marked reviewed.

    Produces the same review_ended state a manual "End Review" does, and
    like that route it calls send_summary_screen directly rather than
    relying on present_current_hunk to redirect there — see that function's
    docstring for why browsing has to keep working afterwards.

    Returns True when this call is what ended the review, so the caller can
    skip its own send_review_progress: send_summary_screen has already sent
    one."""
    if session.hunks and len(session.reviewed) == len(session.hunks) and not session.review_ended:
        session.review_ended = True
        cancel_current(session)
        save_persisted_state(session.repo_path, session)
        await send_summary_screen(ws, session)
        return True
    return False


@handler("toggle_reviewed")
async def handle_toggle_reviewed(ws: WebSocket, session: Session, payload: dict) -> None:
    """Marks or unmarks the current hunk (no payload). Frozen once the
    review has ended, the same gate "Review all", replies and
    "request_change" sit behind. Marking the last unreviewed hunk ends the
    review outright — see _maybe_auto_end_review."""
    if session.review_ended:  # locked once the review has ended — see Session docstring
        return
    if session.current_hunk is None:
        return
    session.reviewed.symmetric_difference_update({session.index})
    save_persisted_state(session.repo_path, session)
    if await _maybe_auto_end_review(ws, session):
        return
    await send_review_progress(ws, session)


@handler("toggle_reviewed_all")
async def handle_toggle_reviewed_all(ws: WebSocket, session: Session, payload: dict) -> None:
    """The "Review all" checkbox — mirrors handle_toggle_reviewed but for
    every hunk in the session at once. Symmetric like the single-hunk
    version: if everything is already reviewed, this clears all of it
    instead of being a one-way ratchet with no way back via this control."""
    if session.review_ended:  # locked once the review has ended — see Session docstring
        return
    if not session.hunks:
        return
    if len(session.reviewed) == len(session.hunks):
        session.reviewed.clear()
    else:
        session.reviewed = set(range(len(session.hunks)))
    save_persisted_state(session.repo_path, session)
    if await _maybe_auto_end_review(ws, session):
        return
    await send_review_progress(ws, session)


async def refresh_diff(ws: WebSocket, session: Session, notice_message: str | None = None) -> None:
    """Re-reads the diff and clears the derived caches, so the walkthrough
    catches up with a change applied outside this app.

    The reviewer triggers it by hand, separately from queuing a comment,
    because this app has no way to know when — or whether — an external edit
    happened. It re-reads everything rather than trying to patch state
    surgically: blunt, but it can't leave the session half-updated.

    notice_message overrides the default text of the "notice" sent below.
    The "new_review" handler reuses this whole function, and there the
    default line about reviewed status carrying over would be wrong: nothing
    carries over on that path, by design."""
    repo_path = CONFIG["server"].get("repo_path", ".")
    try:
        new_hunks = await asyncio.to_thread(get_review_hunks, repo_path)
    except DiffError as exc:
        # Mirrors the identical try/except around the twin call in
        # websocket_endpoint (the initial-connect path) — this one didn't
        # have it, which mattered more than that one does: one of the two
        # routes into this function is handle_confirm_act_now, called
        # inline (not via create_task) right after a file write has already
        # succeeded. An uncaught DiffError here — e.g. a `.git/index.lock`
        # left behind by a concurrent git process — would propagate out of
        # the inline dispatch and kill the whole WebSocket connection,
        # leaving the reviewer unable to tell whether their just-applied
        # change actually landed. Reporting it as an error and returning
        # early keeps the connection (and the write that already happened)
        # intact. Deliberately does NOT touch session.hunks/briefings/
        # narrations/conversation_histories/reviewed below — a failed
        # refresh should leave every cache exactly as it was before the
        # attempt, not half-clear state for a diff read that never
        # actually completed.
        await send_error(
            ws,
            f"Couldn't re-read the diff: {exc}. Any change you just applied was NOT lost — "
            "only this refresh failed. Try Refresh Diff again once the git operation clears.",
        )
        return
    # Reviewed status is keyed by stable_hunk_key (hunk content, not
    # position — see diff_service.py/session_store.py), captured from the
    # *old* hunk list before it's replaced, then re-mapped onto the new
    # one via reconcile_reviewed: a hunk whose own content is unchanged
    # keeps its reviewed mark even if its line numbers shifted; a hunk
    # that actually changed is correctly treated as new/unreviewed again.
    # This is the same reconciliation websocket_endpoint does against the
    # on-disk persisted state at connect time — reused here since a
    # refresh is really "re-derive session.reviewed against a fresh hunk
    # list," just fed from the in-memory set instead of disk.
    old_reviewed_keys = [stable_hunk_key(hunk) for i, hunk in enumerate(session.hunks) if i in session.reviewed]
    session.hunks = new_hunks
    session.briefings.clear()
    session.narrations.clear()
    session.conversation_histories.clear()
    session.reviewed = reconcile_reviewed(new_hunks, old_reviewed_keys)
    save_persisted_state(session.repo_path, session)
    # Drops on-disk briefings that match no current hunk (orphaned by a
    # shifted header, or stale content) and keeps the rest — see
    # prune_briefing_cache for why this is selective rather than a wholesale
    # delete.
    await asyncio.to_thread(prune_briefing_cache, repo_path, new_hunks)
    await send_json(
        ws,
        "notice",
        {
            "level": "success",
            "message": notice_message
            or "Diff refreshed — briefings and reviewed status carried over for unchanged hunks.",
        },
    )

    if not session.hunks:
        session.index = -1
        await send_json(ws, "presenting", {"done": True, "index": -1, "total": 0})
        await send_review_progress(ws, session)
        return

    session.index = min(session.index, len(session.hunks) - 1) if session.index >= 0 else 0
    if session.review_ended:
        # Refreshing doesn't reset review_ended — "new_review" is the one
        # caller that does, and it resets before reaching here — so a
        # refresh while still ended lands back on the wrap-up screen.
        # present_current_hunk doesn't redirect there on its own, so this
        # asks explicitly, same as the initial connect in
        # websocket_endpoint.
        await send_summary_screen(ws, session)
    else:
        session.current_task = asyncio.create_task(present_current_hunk(ws, session))


@handler("refresh_diff", cancels=True, background=True)
async def handle_refresh_diff(ws: WebSocket, session: Session, payload: dict) -> None:
    """Handles the Refresh Diff button (no payload). See refresh_diff
    above."""
    await refresh_diff(ws, session)


@handler("next", cancels=True)
async def handle_next(ws: WebSocket, session: Session, payload: dict) -> None:
    # Deliberately allowed to advance one past the last valid
    # index (current_hunk then returns None) — that's what
    # makes present_current_hunk's "done" branch reachable at
    # all. The old bound (`< len(hunks) - 1`) capped index at
    # len(hunks) - 1 forever, so "done" — and the frontend's
    # "All hunks reviewed" state — could never fire; Next on
    # the last hunk just silently re-presented it.
    if session.index < len(session.hunks):
        session.index += 1
    session.current_task = asyncio.create_task(present_current_hunk(ws, session))


@handler("prev", cancels=True)
async def handle_prev(ws: WebSocket, session: Session, payload: dict) -> None:
    session.index = max(0, session.index - 1)
    session.current_task = asyncio.create_task(present_current_hunk(ws, session))


@handler("stop", cancels=True)
async def handle_stop(ws: WebSocket, session: Session, payload: dict) -> None:
    """The cancel is the whole action — the registry's cancels=True has
    already run cancel_current by the time this is called. "stop" is the
    wire name for what the UI labels "Interrupt"; it cancels whatever is in
    flight, LLM or TTS. Renamed in the UI only — don't rename the message
    to match without updating static/js/settings.js in the same change."""


@handler("jump_to_hunk", cancels=True)
async def handle_jump_to_hunk(ws: WebSocket, session: Session, payload: dict) -> None:
    """Jumps straight to one hunk.

    Payload: {"index": int}. Used by the file-list panel to reach a file's
    first hunk instead of stepping there one Next at a time. An
    out-of-range index is an error rather than a clamp: it means the
    client's file list and the session's hunks have gone out of step."""
    index = payload.get("index")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(session.hunks):
        session.index = index
        session.current_task = asyncio.create_task(present_current_hunk(ws, session))
    else:
        await send_error(ws, "Invalid hunk to jump to.")


async def _warn_about_stale_prep(ws: WebSocket, session: Session) -> None:
    """Once per review, name any prep file that was written at an earlier
    commit.

    Only PRESENT-but-stale files, never missing ones: narration runs fine
    without a prep file, so "you could generate one" is the settings
    panel's business, not a notice. Stale is the case worth interrupting
    for, because the app feeds it to the model as fact — measured here, a
    call map written before a refactor named a deleted module and knew
    nothing of the package that replaced it."""
    status = await asyncio.to_thread(context_status, session.repo_path)
    stale = [key for key, info in status.items() if info.get("present") and info.get("head_moved")]
    if not stale:
        return
    names = ", ".join(_PREP_LABELS.get(key, key) for key in stale)
    hints = " ".join(f"{status[key]['refresh_hint']}." for key in stale)
    await send_json(
        ws,
        "notice",
        {
            "level": "info",
            "message": (
                f"The {names} recorded for this repo predate the current commit, and narration uses "
                f"them as background — they can name code that has since moved or gone. {hints}"
            ),
        },
    )


@handler("start_review")
async def handle_start_review(ws: WebSocket, session: Session, payload: dict) -> None:
    """Starts the review, which is what lets briefing and conversation run
    at all.

    No payload. Before this arrives every hunk still
    shows its code view and Next/Prev/jump_to_hunk all work, but
    present_current_hunk skips briefing and conversation and the client
    shows a "Start Review" button in place of the thinking placeholder.
    Every hunk after this narrates automatically."""
    if not session.review_started:
        session.review_started = True
        save_persisted_state(session.repo_path, session)
        await _warn_about_stale_prep(ws, session)
        cancel_current(session)
        # Re-present the current hunk rather than duplicating the
        # briefing+conversation logic here: this resends
        # "presenting" (harmless — the client treats it
        # idempotently) and then, since review_started is now
        # true and this hunk was never narrated, falls straight
        # into the same briefing+conversation path a fresh hunk
        # load takes once the session has started.
        session.current_task = asyncio.create_task(present_current_hunk(ws, session))
    # else: already started — ignore a duplicate/late click.


@handler("end_review")
async def handle_end_review(ws: WebSocket, session: Session, payload: dict) -> None:
    """Freezes narration, replies and the reviewed marks for the rest of
    the review (no payload) and swaps the code pane to the summary screen —
    reachable from whatever hunk is on screen, not only from past the last
    one. Also reached automatically once every hunk is marked reviewed (see
    _maybe_auto_end_review). review_started and review_ended only ever go
    False -> True on their own; "new_review" is the one way back."""
    if not session.review_ended:
        session.review_ended = True
        save_persisted_state(session.repo_path, session)
        cancel_current(session)
        await send_summary_screen(ws, session)
    # else: already ended — ignore a duplicate/late click.


@handler("show_summary")
async def handle_show_summary(ws: WebSocket, session: Session, payload: dict) -> None:
    # The way back from browsing a hunk post-end (see
    # present_current_hunk's docstring) — the file list, Prev
    # and Next all still work after a review ends, so there
    # needs to be an explicit way back to the wrap-up screen
    # rather than only a forward path away from it.
    if session.review_ended:
        cancel_current(session)
        await send_summary_screen(ws, session)


@handler("new_review")
async def handle_new_review(ws: WebSocket, session: Session, payload: dict) -> None:
    """Resets review_started/review_ended/reviewed and re-reads the diff
    fresh (no payload) — for reviewing a genuinely different set of changes
    against the same repo_path. Only meaningful once the review has ended,
    and only offered there, as "Start New Review" on the summary screen."""
    if session.review_ended:
        cancel_current(session)
        session.review_started = False
        session.review_ended = False
        session.reviewed = set()
        # -1, not left as whatever hunk was on screen when the
        # review ended — refresh_diff's own "clamp to
        # current index" tail (`min(session.index, ...) if
        # session.index >= 0 else 0`) only lands on hunk 0 if
        # it starts negative.
        session.index = -1
        save_persisted_state(session.repo_path, session)
        # Reuses refresh_diff wholesale (same
        # direct-await pattern handle_confirm_act_now already
        # uses) rather than duplicating the re-diff logic — it
        # re-reads the working tree, clears briefings/
        # narrations/conversation histories, reconciles
        # session.reviewed (against the now-empty set just
        # assigned above, so it stays empty — a real fresh
        # start, not a partial carry-over), clears the on-disk
        # briefing cache, and re-presents hunk 0.
        # pending_review_comments/next_comment_id and every
        # .review/review_*.md hand-off plan is untouched by any
        # of this — only .briefing/ gets cleared.
        await refresh_diff(ws, session, notice_message="Starting a new review — the diff was re-read fresh.")
    # else: not ended — nothing to reset; not reachable from the UI.
