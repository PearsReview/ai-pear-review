"""Per-connection review state. Holds data only — every behaviour that reads
or changes it lives in app/handlers/."""

from __future__ import annotations

import asyncio

from ..services.briefing_service import Briefing
from ..services.conversation_service import ConversationClient
from ..services.diff_service import Hunk
from ..services.harness_service import ActNowRequest, ProposedChange


class Session:
    """Per-connection review state: hunk list, position, transcript, live clients.

    conversation is constructed by the caller (websocket_endpoint) rather
    than here, because construction can raise ConversationError (missing
    API key). Unlike a missing diff, that's not fatal to the
    connection — conversation may be None, in which case the walkthrough
    runs in a degraded mode: raw diff shown, no presenter dialogue, no
    briefing calls (nothing would consume their output), replies rejected
    with a clear error instead of silently doing nothing.
    """

    def __init__(self, hunks: list[Hunk], conversation: ConversationClient | None, repo_path: str) -> None:
        self.hunks = hunks
        self.repo_path = repo_path  # so any handler can persist state without re-reading CONFIG itself
        self.index = -1
        self.transcript: list[dict] = []  # [{"role": "presenter"|"reviewer", "text": ...}]
        self.briefings: dict[int, Briefing] = {}  # hunk index -> Briefing, cached across Prev/Next
        self.narrations: dict[int, str] = {}  # hunk index -> narration text, cached across Prev/Next
        self.conversation_histories: dict[int, list[dict]] = {}  # hunk index -> that hunk's own conversation
        # file_path -> that file's own conversation, for "explore mode"
        # (handle_explore_file/handle_explore_reply) — a second bucket,
        # not folded into conversation_histories above, because an
        # explored file has no hunk index to key off (it's never in
        # session.hunks at all — that's the whole point, it hasn't
        # changed). Deliberately not persisted to disk (session_store.py)
        # or cleared by refresh_diff/new_review — explore-mode
        # conversations are about files outside the diff entirely, so
        # nothing about the diff changing has anything to say about them.
        self.file_conversation_histories: dict[str, list[dict]] = {}
        self.reviewed: set[int] = set()  # hunk indices explicitly marked reviewed
        self.conversation = conversation
        self.current_task: asyncio.Task | None = None
        # Reviewer's STT/TTS preference, set by "set_voice_prefs" — distinct
        # from whether the services are reachable, which the STT/TTS client
        # calls report separately. Defaults to on, so a connection that
        # never sends a preference gets speech.
        self.stt_enabled = True
        self.tts_enabled = True
        # Whether moving to an unexplained hunk explains it automatically
        # once the review has started. Off, a hunk is explained only on
        # request ("explain_hunk"). Set from the connection URL's
        # ?auto_narrate=0 (so the first hunk already obeys it) and by
        # "set_narration_prefs" on every change.
        self.auto_narrate = True
        # Comments queued by "request_change" accumulate here until
        # "finish_review" hands the batch off (app/handlers/comments.py).
        # next_comment_id is a monotonic counter, not
        # len(pending_review_comments), so removing a comment can never let
        # a later one reuse its id.
        self.pending_review_comments: list[dict] = []
        self.next_comment_id = 0
        # Act Now: the agent's proposed changes between "act_now" (preview
        # only) and "confirm_act_now" (the only step that writes) — the write
        # always uses exactly what was last previewed, never trusting content
        # sent from the client at confirm time.
        self.pending_act_now: tuple[ProposedChange, ...] | None = None
        # What that preview was asked for, so "refine_act_now" can re-run the
        # agent with the whole request. Set and cleared with pending_act_now.
        self.act_now_request: ActNowRequest | None = None
        # file_path currently being read aloud via "speak_file", or None —
        # status-only (which icon should show a "stop" affordance); the
        # actual cancellability comes from current_task above like every
        # other long-running action, this isn't a second task-tracking
        # mechanism.
        self.tts_reading_file: str | None = None
        # Gates briefing and conversation, never the code view — see
        # present_current_hunk and the "start_review" handler. False until
        # the reviewer explicitly starts the review, and never reset to
        # False once set, so every hunk after that narrates automatically.
        self.review_started = False
        # Freezes narration/replies and "Mark as reviewed"/"Review all" for
        # the rest of this review — see present_current_hunk's early
        # send_summary_screen branch and the "end_review" handler. Set
        # either explicitly ("end_review") or automatically once every
        # hunk is reviewed (see handle_toggle_reviewed/_all). Like
        # review_started, both flags are loaded from session_store.py at
        # connect time, so an ended (or in-progress) review stays that way
        # across a reconnect or a full app restart, not just this
        # connection.
        self.review_ended = False

    @property
    def current_hunk(self) -> Hunk | None:
        if 0 <= self.index < len(self.hunks):
            return self.hunks[self.index]
        return None
