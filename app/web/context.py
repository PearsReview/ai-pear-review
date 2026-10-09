"""Builds the extra prompt text a model call carries beyond the reviewer's
own words: repo-level project context, the change's shape, marked-line
excerpts, and the token budgeting that decides how much history fits.
Pure functions of session data and prep files — never calls a model and
never sends anything to the client.

"marked_lines" — the optional field on "reply" and "request_change" that
marked_lines_context and line_context below consume: [{"file_path",
"old_lineno", "new_lineno", "text", "kind"}, ...]. It comes from
double-clicking a line in the code view to toggle an Eclipse-style marker.
At most 2 markers can be active, but with 2 in the same file the frontend
expands them to every line in between before sending, so this list is 1..N
entries — not 1 or 2 (see buildMarkedContext in static/js/interactions.js). Only ever
used to build extra hidden context for the model call — prepended to the
prompt, never shown as the visible transcript text — or the change-request
text, the same "hidden context, visible instruction" pattern
editor_service.build_change_request uses. The line text comes from the
client, which already had it rendered, rather than being re-read from disk
here: a marked "del" line may no longer exist in the working tree at all."""

from __future__ import annotations

from collections.abc import Callable

from ..services.briefing_service import Briefing, ChangeContext, RelatedHunk
from ..services.changeset import theme
from ..services.diff_service import Hunk
from ..services.editor_service import code_fence, hunk_line_range
from ..services.project_overview import overview_prompt_block
from .session import Session

# How much bigger the context caps may be for a provider that manages its
# own window. Every cap in the context modules was measured against the
# local default: qwen-7b at num_ctx 8192, where ~900 characters of
# background is a real fraction of what the diff needs. 6x turns that into
# ~5400 characters — still under 1% of a 1M-token window, so it buys
# context without ever being the reason a prompt doesn't fit.
_LARGE_WINDOW_SCALE = 6


def context_scale(session: Session) -> int:
    """Multiplier for the context caps, given what this session can afford.

    Keyed on num_ctx rather than prompt_budget, even though the two mostly
    agree: prompt_budget also returns None when there is no conversation at
    all (degraded mode), and answering "you have a huge window" for a
    session with no model is the wrong reading of the same absence.
    Deriving one multiplier here keeps the provider question in web/ — no
    module under services/ has to know which provider is configured, it
    just gets told how much room there is."""
    conversation = getattr(session, "conversation", None)
    if conversation is None:
        return 1  # nothing is sent in degraded mode; don't widen caps for it
    return 1 if getattr(conversation, "num_ctx", None) else _LARGE_WINDOW_SCALE


def build_project_context(session: Session, hunk: Hunk) -> str | None:
    """The repo-level background for one hunk's opening narration turn:
    what the project is, what this file is for, and where this hunk sits
    in the change.

    The call-map skill's callers block is no longer included: the map only
    covers Python, and callers are to come from the coding agent instead
    (see vscode/TODO.md).

    Assembled here rather than inside ConversationClient because every
    piece of it comes from a file some .claude/ skill wrote — the app
    reads prep work, it never generates it (see briefing_service.py on why
    the app never drives a CLI itself). Both blocks are already sliced and
    budget-capped by their own modules; this only decides the order.

    Order is deliberate: the overview frames everything after it.

    Returns None when no skill has run, which is the common case and is
    exactly the pre-skill behaviour.
    """
    scale = context_scale(session)
    blocks = [
        overview_prompt_block(session.repo_path, hunk.file_path, scale),
        change_shape_block(session.hunks, hunk),
    ]
    present = [block for block in blocks if block]
    return "\n\n".join(present) if present else None


# How many other changed files to name before summarising. Enough to show
# the shape of a change, few enough that a 40-file refactor doesn't spend
# the diff's budget listing paths.
_MAX_NAMED_FILES = 6


def change_shape_block(hunks: list[Hunk], hunk: Hunk) -> str | None:
    """Where this hunk sits in the change as a whole.

    The model is shown one hunk and, left to itself, treats it as the
    entire change — so a hunk that is one leg of a rename across nine files
    gets explained as if it were a standalone edit. This is the cheapest
    context in the app: no skill, no file, no LLM call, just what the
    session already holds.

    Facts only, and an explicit instruction not to speculate. Naming files
    the model cannot see is an invitation to invent relationships between
    them.
    """
    if len(hunks) <= 1:
        return None  # a single-hunk change has no shape to describe

    files = list(dict.fromkeys(h.file_path for h in hunks))  # stable order, deduped
    others = [path for path in files if path != hunk.file_path]

    position = f"This is hunk {hunk.index + 1} of {len(hunks)} in the change being reviewed"
    if not others:
        return position + f", all of it in {hunk.file_path}."

    named = others[:_MAX_NAMED_FILES]
    listed = ", ".join(named)
    if len(others) > len(named):
        listed += f", and {len(others) - len(named)} more"
    return (
        f"{position}, which spans {len(files)} files. "
        f"Besides {hunk.file_path}, it also touches: {listed}. "
        "You cannot see those files, so do not guess what they contain or how "
        "they relate — mention the wider change only if this hunk makes it obvious."
    )


def _marked_lines_where(marked_lines: list[dict]) -> str:
    """Describes a marked range as "line 42" or "lines 42-45".

    Shared by marked_lines_context, for chat-turn context, and line_context,
    for queued review comments, so the two always word it the same way."""
    first_no = marked_lines[0].get("new_lineno") or marked_lines[0].get("old_lineno")
    last_no = marked_lines[-1].get("new_lineno") or marked_lines[-1].get("old_lineno")
    return f"line {first_no}" if len(marked_lines) == 1 or first_no == last_no else f"lines {first_no}-{last_no}"


_DIFF_PREFIX = {"add": "+", "del": "-"}


def marked_lines_context(marked_lines: list[dict] | None) -> str | None:
    """Builds a "Regarding lines X-Y of file — a diff excerpt: ```...```"
    block from a marked_lines payload — or None if there's nothing to add.
    Text comes straight from the client (see "marked_lines" in the module
    docstring); this is prompt context, not a security-sensitive value,
    same trust level as human_text itself.

    Reconstructs real +/- diff markers from each line's "kind", falling
    back to unprefixed context when that key is absent, and says outright
    that this is a diff excerpt. Handing the model bare adjacent lines that
    read as final source is what made an old/new value pair look like a
    literal duplicate key. Mirrors how _hunk_prompt frames the whole-hunk
    case."""
    if not marked_lines:
        return None
    file_path = marked_lines[0].get("file_path", "?")
    code_block = "\n".join(f"{_DIFF_PREFIX.get(line.get('kind'), ' ')}{line.get('text', '')}" for line in marked_lines)
    where = _marked_lines_where(marked_lines)
    fence = code_fence(code_block)
    return (
        f"Regarding {where} of {file_path} — a diff excerpt the reviewer selected "
        f"(lines starting with + were added, - were removed, unprefixed lines are "
        f"unchanged context):\n{fence}\n{code_block}\n{fence}"
    )


def augment_with_marked_context(human_text: str, marked_lines: list[dict] | None) -> str:
    context = marked_lines_context(marked_lines)
    return f"{context}\n\n{human_text}" if context else human_text


# ~4 characters per token is the standard rough English/code ratio. A real
# tokenizer would be exact, but would mean shipping one per provider (and a
# dependency this app otherwise doesn't need) to compute a number that only
# ever feeds a safety margin. Deliberately an OVER-estimate in practice:
# being wrong in the direction of "trim a bit early" costs one turn of
# history, while being wrong the other way costs a silent front-truncation,
# which is the failure this exists to prevent.
_CHARS_PER_TOKEN = 4
# Held back from the window for the reply itself plus estimation error.
# num_predict is generation, which shares the same window as the prompt.
_CONTEXT_SAFETY_MARGIN_TOKENS = 512


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(m.get("content", "")) for m in messages)


def prompt_budget(session: Session) -> int | None:
    """Tokens available for system prompt + history + the new message, or
    None when there's no window to budget against (the Anthropic path,
    which manages its own, or an unset num_ctx)."""
    conversation = session.conversation
    num_ctx = getattr(conversation, "num_ctx", None) if conversation else None
    if not num_ctx:
        return None
    reserve = (conversation.max_tokens or 0) + _CONTEXT_SAFETY_MARGIN_TOKENS
    return max(256, num_ctx - reserve)


def fit_history_to_budget(
    history: list[dict], incoming_text: str, system_prompt: str, budget: int | None
) -> tuple[list[dict], int]:
    """Trims a per-hunk history down to what fits, returning
    (kept_history, dropped_count).

    Drops from the MIDDLE, not the front. The first user message is the
    one carrying the hunk's diff (see conversation_service._hunk_prompt),
    so front-trimming — which is exactly what Ollama does on its own when
    a prompt overflows — throws away the grounding and leaves the model
    answering about code it can no longer see. That's the documented cause
    of a reply confidently describing database-query optimisation for a
    diff that only added a farewell() function. Keeping the first message
    and the most recent turns preserves both grounding and continuity;
    what goes is the middle of a long conversation, which is the least
    load-bearing part of it.

    Trimming affects only what's SENT. The transcript the reviewer reads is
    separate state (session.transcript and the client's own DOM) and is
    never touched — dropping a turn from the prompt must not make it
    vanish from the conversation they're having.
    """
    if budget is None:
        return history, 0
    overhead = estimate_tokens(system_prompt) + estimate_tokens(incoming_text)
    if overhead + _messages_tokens(history) <= budget:
        return history, 0
    if len(history) <= 2:
        # Nothing safe left to drop: whatever remains is the diff and/or
        # the immediate exchange. Caller decides what to do about it (see
        # handle_reply's too-large path) rather than silently shipping an
        # over-budget prompt.
        return history, 0

    kept_first = history[:1]
    tail = history[1:]
    dropped = 0
    while tail and overhead + _messages_tokens(kept_first + tail) > budget and len(tail) > 1:
        tail = tail[1:]
        dropped += 1
    return kept_first + tail, dropped


def exceeds_budget(budget: int | None, system_prompt: str, history: list[dict], incoming_text: str) -> bool:
    """Whether this exact prompt would overflow the window even after
    fit_history_to_budget has done what it can. Sending it anyway is the
    worst outcome on Ollama: the front is dropped silently (system prompt,
    briefing, the start of the diff), and on CPU the call can outlast
    timeout_seconds before a single token comes back."""
    if budget is None:
        return False
    return estimate_tokens(system_prompt) + _messages_tokens(history) + estimate_tokens(incoming_text) > budget


def line_context(hunk: Hunk, marked_lines: list[dict] | None) -> tuple[str, str, str, dict | None]:
    """Describes what a review comment is about.

    Returns (file_path, where, snippet, anchor), taken from marked_lines
    when given and otherwise from the current hunk's own diff. A
    marked-lines request may be about a different file than the hunk on
    screen; the caller anchors on a matching hunk for its own purposes,
    while this describes the marked location itself.

    "where" is a short line-range description such as "lines 42-45";
    "snippet" is the code text. Both go straight into the queued comment
    and, later, into the finished document. "anchor" is the same range as
    structured old/new line numbers, or None for the whole-hunk fallback,
    which has no single line to anchor to. static/js/code-view.js uses it to
    re-attach the comment to the right rows on every re-render, which the
    human-readable "where" string alone cannot drive."""
    if marked_lines:
        file_path = marked_lines[0].get("file_path", hunk.file_path)
        snippet = "\n".join(line.get("text", "") for line in marked_lines)
        where = _marked_lines_where(marked_lines)
        anchor = {
            "first_old_lineno": marked_lines[0].get("old_lineno"),
            "first_new_lineno": marked_lines[0].get("new_lineno"),
            "last_old_lineno": marked_lines[-1].get("old_lineno"),
            "last_new_lineno": marked_lines[-1].get("new_lineno"),
        }
        return file_path, where, snippet, anchor
    where = hunk_line_range(hunk.header) or "the current hunk"
    return hunk.file_path, where, hunk.diff_context, None


def change_context(
    session: Session, briefing: Briefing, load_briefing: Callable[[Hunk], Briefing | None]
) -> ChangeContext | None:
    """The theme and related hunks a usable briefing points at, resolved
    against the live diff. None when the briefing is unusable or points at
    nothing that resolves.

    A related entry matches by file_path + header first, then falls back to
    the first hunk in that file: headers shift whenever lines move above a
    hunk, and "the hunk in narration.py" is still the right place to send
    the reviewer. An entry whose file isn't in the diff at all is dropped —
    a link to nothing is worse than no link.

    load_briefing is BriefingClient.load_cached, passed in rather than
    imported so this module stays free of the process-wide clients. Reads
    files, so callers run this off the event loop."""
    if not briefing.usable:
        return None
    found = theme(session.repo_path, briefing.theme)
    related = []
    for entry in briefing.related:
        index = _resolve_hunk(session.hunks, entry["file_path"], entry.get("header"))
        if index is None:
            continue
        target = load_briefing(session.hunks[index])
        summary = target.summary if target is not None and target.usable else None
        related.append(RelatedHunk(index, entry["file_path"], entry["relation"], entry.get("note"), summary))
    if found is None and not related:
        return None
    return ChangeContext(
        theme_title=found["title"] if found else None,
        theme_why=found["why"] if found else None,
        related=tuple(related),
    )


def _resolve_hunk(hunks: list[Hunk], file_path: str, header: str | None) -> int | None:
    first_in_file = None
    for i, hunk in enumerate(hunks):
        if hunk.file_path != file_path:
            continue
        if header and hunk.header == header:
            return i
        if first_in_file is None:
            first_in_file = i
    return first_in_file
