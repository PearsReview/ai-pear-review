"""The live conversational persona — present_hunk() / respond_to_reviewer().

This module owns conversation policy; talking to a model is delegated to a
provider adapter from app/providers/, picked via config.yaml's
conversation.provider (see app/providers/base.py for the split).

Conversation history is per-hunk, or per-file for answer_about_file, never
per-session. Callers pass a `history: list[dict]` in and get a new list
back; nothing here mutates the caller's list. See _send's docstring for
why that matters: asyncio.to_thread cancellation doesn't stop the worker
thread, so an in-place mutation could still land after the caller had
moved on.

The caller owns that state — Session.conversation_histories keyed by hunk
index, or Session.file_conversation_histories keyed by file_path for a
file explored outside any hunk (app/web/session.py) — and commits the
returned list only once its own await has returned. This client keeps no
message state of its own, just connection details and cumulative usage
counters. Scoping history per hunk is what stops it growing unbounded,
which was the measured cost and latency driver; narration caching in
app/handlers/narration.py is what stops a revisit paying twice.

No provider gets tool access here. That is what keeps this path fast;
investigative work belongs to briefing_service.py.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import TYPE_CHECKING

from ..prompts import PromptSet, select_prompts
from ..providers import (
    Capability,
    ChatProvider,
    Completion,
    ConversationCancelled,
    ConversationError,
    build_provider,
    provider_class,
)
from ..utils.debug_files import write_debug_prompt
from .diff_service import Hunk

if TYPE_CHECKING:
    # Type-hint only (see `from __future__ import annotations` above,
    # which makes annotations lazy strings) — a real import here would
    # create a circular import, since briefing_service.py now imports
    # ConversationClient/ConversationError from this module too.
    from .briefing_service import Briefing, ChangeContext

# Defensive cap on how much of an explored (unchanged) file gets embedded
# in a single prompt (see _file_prompt) — nothing else in this app hands a
# whole arbitrary file to the model (a hunk's diff_context is inherently
# bounded; this isn't), so there's no existing size guard to inherit.
# Generous enough for the overwhelming majority of source files while
# still bounding the worst case (a generated/vendored file the reviewer
# happens to click on) to a fixed, predictable prompt size.
_MAX_FILE_PROMPT_LINES = 800

# Re-exported so existing `from .conversation_service import ConversationError`
# call sites keep working; the classes live with the adapters that raise them.
__all__ = ["ConversationCancelled", "ConversationClient", "ConversationError", "check_conversation_available"]


def check_conversation_available(config: dict) -> bool:
    """Cheap startup check, provider-aware — a boolean, no client
    construction (that's per-Session, not module-level). briefing_service.py
    has no probe of its own: it rides whichever conversation connection this
    check already validated. An unknown provider is simply unavailable."""
    try:
        cls = provider_class(config)
    except ConversationError:
        return False
    return cls.available(config.get(cls.name, {}))


def _briefing_lines(
    briefing: Briefing | None, change_context: ChangeContext | None = None, *, diff_hidden: bool = False
) -> list[str]:
    """The part of a briefing that may enter a prompt, shared by the normal
    and the briefing-only hunk prompts so the trust rules below live once.

    diff_hidden is True when the model can't see the diff (see
    briefing_only_prompt). The skill's summary and each related hunk's
    summary are only sent then, or when briefing.diff_misleads: next to a
    diff that speaks for itself, a prose summary of it is just something
    for a small model to repeat back, or to believe over the code."""
    # Only hand over briefing context when it actually found something —
    # confidence "low" — or no briefing at all, because it failed —
    # means present with just the raw diff.
    if briefing is None or not briefing.usable:
        return []
    lines = [f"\nWhat you actually know about why this looks this way: {briefing.intent}"]
    if briefing.alternatives_considered:
        lines.append(f"Alternatives you considered: {briefing.alternatives_considered}")
    # risk_notes is trusted only from the prep-review skill, never
    # from the app's own local briefing.
    #
    # Whatever is injected here is repeated to the reviewer as
    # established fact about their own code — often close to word
    # for word — so the bar is whether the author of the note could
    # actually have known. A Claude Code session with the repo,
    # git history and tools could. A 7B model handed one hunk and
    # nothing else could not, and measurement bore that out: across
    # a 2x2 of prompt wording and schema constraint it identified
    # real risks only when completely unconstrained, and in that
    # state also fired on 3 of 4 trivial hunks and asserted that
    # `max()` "could have a higher time complexity". It has two
    # modes — write something, or write nothing — with no judgment
    # in between, so the local model doesn't produce the field at
    # all (see briefing_service._BRIEFING_SCHEMA).
    if briefing.risk_notes and briefing.source == "prep-review-skill":
        lines.append(f"Anything you're not fully sure about: {briefing.risk_notes}")

    describe = diff_hidden or briefing.diff_misleads
    if briefing.summary and describe:
        lines.append(f"What this change does: {briefing.summary}")
    if change_context is not None:
        if change_context.theme_why:
            lines.append(
                f"The larger change this is part of ({change_context.theme_title}): {change_context.theme_why}"
            )
        if change_context.related:
            entries = []
            for related in change_context.related:
                entry = f"- {related.relation.replace('_', ' ')} {related.file_path}"
                if related.note:
                    entry += f" ({related.note})"
                if describe and related.summary:
                    entry += f": {related.summary}"
                entries.append(entry)
            lines.append(
                "Other hunks in this change it belongs with (you can't see their code, "
                "so don't guess beyond what's written here):\n" + "\n".join(entries)
            )
    return lines


class ConversationClient:
    """One instance per WebSocket session. Holds a provider adapter and
    cumulative usage counters only — conversation history is owned by the
    caller (see module docstring) and passed into present_hunk()/
    respond_to_reviewer() explicitly. Constructing this is where a missing
    API key (anthropic provider) or an unknown provider surfaces, so callers
    should expect ConversationError right at session start, not just on the
    first call — the ollama provider can't fail this early (no synchronous
    handshake at construction time; an unreachable server surfaces on first
    use instead, same shape as any other ConversationError)."""

    def __init__(self, config: dict, debug_config: dict | None = None) -> None:
        # Each provider's settings live under its own config key, so switching
        # `provider` can never send one provider's model name to the other's
        # API (code review P4) — build_provider hands each adapter only its block.
        self._provider: ChatProvider = build_provider(config)
        # Possibly networked (Ollama asks the server what the model is) —
        # which is why server.py's websocket_endpoint constructs this client off the event loop.
        self._provider.prepare()
        # Per-provider first, shared value as the fallback. 300 is right for a
        # local model's short spoken turn, but on an Anthropic model the same
        # cap is shared with adaptive thinking: replies came back
        # stop_reason=max_tokens, cut off mid-sentence, and when thinking used
        # the whole cap there was no text block at all (see the truncation
        # check in providers/anthropic.py).
        provider_block = config.get(self._provider.name) or {}
        self.max_tokens = provider_block.get("max_tokens") or config.get("max_tokens", 300)
        self.timeout = config.get("timeout_seconds", 60)
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._inflight_cancel: threading.Event | None = None
        # TEST-ONLY prompt/response capture — see config.yaml's `debug:`
        # block and _capture_call below. debug_config is the whole
        # top-level `debug:` mapping (server.py's websocket_endpoint passes it separately from
        # `conversation:`, which is what `config` above is), so this stays
        # None — and every capture a no-op — for any caller that doesn't
        # opt in, which is every real run.
        debug_config = debug_config or {}
        self._capture_dir = (
            debug_config.get("capture_dir", ".llm_calls") if debug_config.get("capture_llm_calls") else None
        )
        self._capture_seq = 0
        # A ConversationClient is built per WebSocket connection, so the
        # sequence above restarts at 1 for every connection — filenames keyed
        # on it alone would have each new connection overwrite the last one's
        # captures (qa_agent opens a fresh connection per test, and the app
        # can have two open at once anyway). The per-client prefix keeps
        # every file distinct while the sequence still orders calls within
        # one connection.
        self._capture_id = uuid.uuid4().hex[:8]
        # Which prompt set (persona/briefing) this connection uses
        # — see app/prompts/. Resolved
        # once here rather than per-call: it depends only on config and the
        # model, neither of which changes mid-connection.
        self.prompts: PromptSet = select_prompts(config, self._provider.prompt_tier())

    # Read-only views of the adapter, for callers that report or budget
    # against them (app/web/context.py's prompt_budget, capture records, tests).
    @property
    def provider(self) -> str:
        return self._provider.name

    @property
    def model(self) -> str:
        return self._provider.model

    @property
    def num_ctx(self) -> int | None:
        return self._provider.num_ctx

    def arm_cancel(self) -> threading.Event:
        """Hands back a fresh cancellation token for the call about to
        start, and makes it this client's current one.

        Call it from the event loop immediately before dispatching a model
        call into a worker thread — app/web/runtime.py's run_llm does
        exactly this — and set the token to abort that call.

        A new Event per call, never one shared Event cleared and reused. A
        shared token would be silently wrong in the exact case this exists
        for: cancel a call, start the next, and clearing the flag
        un-cancels the orphaned thread still polling it, keeping the
        abandoned request alive. A per-call token can only be cancelled,
        never resurrected.

        Safe without locking because ConversationClient is constructed per
        WebSocket connection (server.py) and Session.current_task holds
        exactly one task, so at most one call per client is ever in flight;
        this is only ever reassigned from the event loop thread, and the
        worker thread only reads its own token."""
        cancel = threading.Event()
        self._inflight_cancel = cancel
        return cancel

    def present_hunk(
        self,
        history: list[dict],
        hunk: Hunk,
        briefing: Briefing | None,
        project_context: str | None = None,
        change_context: ChangeContext | None = None,
    ) -> tuple[list[dict], str]:
        """Opens (or continues) the conversation for one hunk: builds the
        presentation prompt from the hunk's diff plus, if confident enough,
        the briefing's intent/alternatives/risk notes, plus (optionally)
        a short project-level note (see _hunk_prompt), and sends it.
        Returns (new_history, reply_text) — see _send's docstring for why
        history is returned rather than mutated.

        project_context is already sliced and budget-capped by the caller
        (services/project_overview.py) — this method neither trims it nor
        decides what's in it."""
        return self._send(history, self._hunk_prompt(hunk, briefing, project_context, change_context))

    def generate_once(
        self,
        prompt: str,
        system_prompt: str,
        max_tokens: int | None = None,
        response_schema: dict | None = None,
    ) -> str:
        """One-shot, historyless call — for briefing generation and Act
        Now, not the ongoing per-hunk conversation. Doesn't touch any
        persistent history (present_hunk/respond_to_reviewer's callers own
        that); does still count toward total_input_tokens/
        total_output_tokens like any other call, since it's real usage
        against the same connection.

        response_schema, if given, is a JSON Schema object — passed through
        to Ollama's `format` field (grammar-constrained decoding: the
        response is guaranteed syntactically valid JSON matching the
        schema, which prompting alone does not achieve on a small local
        model). Confirmed empirically on this app's own default model
        (llama3.2:3b): a free-text SEARCH/REPLACE marker template for Act
        Now was followed correctly well under half the time, while the
        equivalent {"search", "replace"} JSON schema was ~4x more
        reliable. Dropped for any provider without Capability.JSON_SCHEMA
        (see _complete); extract_json_object() (app/utils/json_utils.py) is
        the fallback there."""
        return self._complete([{"role": "user", "content": prompt}], system_prompt, max_tokens, response_schema)

    def respond_to_reviewer(
        self, history: list[dict], human_text: str, from_voice: bool = False
    ) -> tuple[list[dict], str]:
        """Continues an already-opened conversation with the reviewer's
        next turn. from_voice=True wraps human_text with a note that it was
        transcribed (see _wrap_if_from_voice) rather than typed, since a
        garbled transcription needs different handling than a garbled typo.
        Returns (new_history, reply_text), same contract as present_hunk."""
        return self._send(history, self._wrap_if_from_voice(human_text, from_voice, topic="this hunk"))

    def answer_about_file(
        self,
        history: list[dict],
        file_path: str,
        file_content: str,
        human_text: str,
        from_voice: bool = False,
    ) -> tuple[list[dict], str]:
        """Same shape as present_hunk/respond_to_reviewer, for a file the
        reviewer is asking about outside any hunk (see "explore mode" —
        app/handlers/explore.py's handle_explore_file/handle_explore_reply). An empty
        history means this is the first question about this file, so the
        file itself has to be embedded in the prompt now (see _file_prompt)
        the same way present_hunk embeds the diff — every later turn
        reaches this with a non-empty history and just needs the reviewer's
        next question, the file's content already sitting in it from turn
        one. Deliberately NOT gated behind review_started/review_ended
        anywhere in the caller — asking about unchanged code is meant to
        work before a review even starts."""
        # Wrap the reviewer's actual question first, not the whole embedded-
        # file prompt below — only the question was transcribed from voice,
        # so a wrapping note around the file content too would misleadingly
        # imply the file itself might be a mistranscription.
        question = self._wrap_if_from_voice(human_text, from_voice, topic="this file")
        prompt_text = self._file_prompt(file_path, file_content, question) if not history else question
        return self._send(history, prompt_text)

    def _wrap_if_from_voice(self, text: str, from_voice: bool, topic: str) -> str:
        """Speech-to-text is fallible — this is a best-effort nudge, not a
        guarantee: whether the model actually notices a mismatch and asks
        for clarification instead of confidently answering a garbled
        transcription depends on it following this instruction, the same
        reliability caveat as everything else asked of it in this system
        prompt (see app/prompts/'s persona_system confidence-calibration
        notes). There's no deterministic way to catch "this doesn't fit the
        topic" the way the exact-phrase hallucination filter in
        voice_service.py catches known Whisper artifacts — topic relevance
        needs actual judgment."""
        if not from_voice:
            return text
        return (
            "(This was transcribed from the reviewer's voice — it may "
            "contain transcription errors. If it doesn't make sense for "
            f"{topic}, or seems unrelated to what's being discussed, "
            "say so and ask them to repeat or confirm rather than "
            f"guessing at what they meant.)\n\n{text}"
        )

    def _send(self, history: list[dict], user_text: str) -> tuple[list[dict], str]:
        """Returns a NEW history list rather than mutating the caller's in
        place. This runs inside asyncio.to_thread (see run_llm), and
        to_thread cancellation doesn't stop the underlying worker thread —
        a call already in flight when the reviewer hits Interrupt/Next
        keeps running to completion in the background regardless. The old
        in-place-mutation version meant that "orphaned" completion still
        silently appended a turn to session.conversation_histories's
        shared list, one nobody asked for and that session.narrations
        never got a matching update for (that assignment happens in
        the handler, after the await — which a cancellation skips). Handing
        back a new list means an orphaned call's result just isn't
        anything the caller ever reads: the code path that would commit it
        into session state never runs once the awaiting coroutine is
        cancelled, and the shared list was never touched to begin with."""
        new_history = history + [{"role": "user", "content": user_text}]
        text = self._complete(new_history, self.prompts.persona_system)
        new_history.append({"role": "assistant", "content": text})
        return new_history, text

    def _complete(
        self,
        messages: list[dict],
        system_prompt: str,
        max_tokens: int | None = None,
        response_schema: dict | None = None,
    ) -> str:
        """The single choke point every call goes through — _send and
        generate_once alike — so capability gating, capture and usage
        counting can't be missed by any call site."""
        cancel = self._inflight_cancel
        if cancel is not None and cancel.is_set():
            # Abandoned before the request was even issued — don't take a
            # provider slot at all.
            raise ConversationCancelled("cancelled before dispatch")
        if not self._provider.capabilities & Capability.JSON_SCHEMA:
            response_schema = None
        result: Completion = self._provider.complete(
            messages, system_prompt, max_tokens or self.max_tokens, response_schema, cancel
        )
        self._capture_call(
            system_prompt, messages, result.text, result.input_tokens, result.output_tokens, response_schema
        )
        if not result.text:
            raise ConversationError(f"{self.provider} returned no text content")
        self.total_input_tokens += result.input_tokens
        self.total_output_tokens += result.output_tokens
        return result.text

    def _capture_call(
        self,
        system_prompt: str,
        messages: list[dict],
        response: str,
        in_tokens: int,
        out_tokens: int,
        response_schema: dict | None = None,
    ) -> None:
        """TEST-ONLY (config.yaml's debug.capture_llm_calls, off by
        default): writes one JSON file per LLM call with the exact system
        prompt, message list and response.

        Called from _complete, the single choke point every call goes
        through — narration, replies, explore answers and briefings
        alike — so no call site can be missed and no per-caller plumbing is
        needed.

        This exists for qa_agent (see qa_agent/llm_capture.py): its judges
        score this app's output, and a verdict is only as useful as the
        record of what the model was actually given. Nothing in the app
        reads these files back — they are purely an outbound diagnostic.

        Best-effort via write_debug_prompt, which swallows OSError: a
        capture failure must never break the call it's recording. JSON is
        written as text through that same helper rather than duplicating
        its mkdir/try-except convention here."""
        if not self._capture_dir:
            return
        self._capture_seq += 1
        record = {
            "seq": self._capture_seq,
            "connection": self._capture_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "provider": self.provider,
            "model": self.model,
            "system_prompt": system_prompt,
            "messages": messages,
            "response": response,
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "response_schema": response_schema,
        }
        write_debug_prompt(
            self._capture_dir, f"{self._capture_id}_{self._capture_seq:04d}.json", json.dumps(record, indent=2)
        )

    def _hunk_prompt(
        self,
        hunk: Hunk,
        briefing: Briefing | None,
        project_context: str | None = None,
        change_context: ChangeContext | None = None,
    ) -> str:
        parts = []
        # Project background first, before the diff: it frames everything
        # after it, and it's the part most worth keeping if the provider
        # ever has to truncate (it truncates from the front, so "first"
        # is really "most protected" only in the sense that it's what the
        # budgeting in app/web/context.py works hardest to preserve — see
        # fit_history_to_budget).
        if project_context:
            parts.extend([project_context, ""])
        parts.extend([f"File: {hunk.file_path}", "", f"Diff hunk:\n```\n{hunk.diff_context}\n```"])
        parts.extend(_briefing_lines(briefing, change_context))
        # Repeated here, last, because the system prompt's length rule alone
        # was overrun (~110 words against "about 60") once a briefing and a
        # theme gave the model plenty to retell.
        parts.append("\nPresent this hunk now, in two or three short sentences.")
        return "\n".join(parts)

    def present_from_briefing(
        self, hunk: Hunk, briefing: Briefing, change_context: ChangeContext | None = None, reason: str = "too_large"
    ) -> tuple[list[dict], str]:
        """Narrates a hunk from its briefing and change context, without
        the diff.

        For a hunk too large for the window (reason "too_large") or whose
        normal narration call failed ("call_failed"). Otherwise
        present_hunk.
        Historyless — the returned history starts the hunk's conversation
        without the diff, so later replies fit."""
        return self._send([], self.briefing_only_prompt(hunk, briefing, change_context, None, reason))

    def answer_from_briefing(
        self,
        hunk: Hunk,
        briefing: Briefing | None,
        question: str,
        change_context: ChangeContext | None = None,
        reason: str = "too_large",
        from_voice: bool = False,
    ) -> tuple[list[dict], str]:
        """Answers a question from the briefing and change context, without
        the diff.

        Uses any reviewer-selected lines already folded into `question` (see
        augment_with_marked_context). Historyless, like
        present_from_briefing. Otherwise respond_to_reviewer."""
        question = self._wrap_if_from_voice(question, from_voice, topic="this hunk")
        return self._send([], self.briefing_only_prompt(hunk, briefing, change_context, question, reason))

    def briefing_only_prompt(
        self,
        hunk: Hunk,
        briefing: Briefing | None,
        change_context: ChangeContext | None,
        question: str | None,
        reason: str,
    ) -> str:
        """Public so the caller can check this prompt fits before sending it.

        States outright that the diff is absent. Left implicit, the persona
        prompt's "base everything on the diff in front of you" plus a line
        count is an invitation to describe code the model never saw."""
        added = sum(1 for line in hunk.lines[1:] if line.startswith("+"))
        removed = sum(1 for line in hunk.lines[1:] if line.startswith("-"))
        absent = (
            "This hunk is too large to show you: its diff is NOT included here."
            if reason == "too_large"
            else "The diff for this hunk is NOT included here."
        )
        parts = [
            f"File: {hunk.file_path}",
            f"Hunk: {hunk.header} ({added} lines added, {removed} removed)",
            "",
            f"{absent} Do not describe, quote or guess at code you cannot see.",
        ]
        parts.extend(_briefing_lines(briefing, change_context, diff_hidden=True))
        if question is None:
            admit = (
                "the diff itself was too large for you to read"
                if reason == "too_large"
                else "you're working from notes about the change, not the diff itself"
            )
            parts.append(f"\nPresent this hunk now, using only what is above, and say plainly that {admit}.")
        else:
            parts.append(
                f"\nReviewer's question: {question}\n\nAnswer using only what is above. "
                "If it doesn't cover the question, say so plainly rather than guessing."
            )
        return "\n".join(parts)

    def _file_prompt(self, file_path: str, file_content: str, question: str) -> str:
        """Grounds the first turn of an explore-mode conversation (see
        answer_about_file) — unlike _hunk_prompt, there's no diff and no
        briefing, just the file as it stands. Truncated defensively at
        _MAX_FILE_PROMPT_LINES: a reviewer clicking through an "all files"
        browser can land on anything, including a huge generated/vendored
        file never meant to be read whole."""
        lines = file_content.splitlines()
        truncated = len(lines) > _MAX_FILE_PROMPT_LINES
        if truncated:
            lines = lines[:_MAX_FILE_PROMPT_LINES]
        body = "\n".join(lines)
        if truncated:
            body += f"\n... [truncated after {_MAX_FILE_PROMPT_LINES} lines]"
        return (
            f"File: {file_path}\n\n"
            "This file has NOT changed in the current diff — the reviewer "
            "is asking about it purely for context, not reviewing a change "
            f"here. Full file content:\n```\n{body}\n```\n\n"
            f"Reviewer's question: {question}"
        )
