"""Background facts looked up for one reviewer question, chosen by what
the question asks — rather than injected into every prompt.

A small model on CPU pays for every token in the window, and a narration
already carries project context and the briefing. So the facts below are
fetched only when a reply's question makes them relevant:

    "why was this changed?"    -> commit subjects for these lines, when no briefing says why
    "what does `foo` do?"      -> foo's definition, for identifiers the hunk mentions

"Who calls this?" and "is this tested?" used to be answered from the
call-map skill's .context/call_map.json. That is switched off: the map
only covers Python, and these questions are to move to the coding agent
(see vscode/TODO.md). Until then they get no extra context.

Routing is keyword matching, deliberately not a model call: classifying
the question with the same CPU model would double the wait for a reply.
A missed route costs nothing (the reply works as before); a wrong one
costs a few hundred characters, capped by _MAX_CHARS.

Reads files and runs git, so handlers call this off the event loop. Never
calls a model and never sends anything to the client.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..services.code_search import Definition, DefinitionNotFound, find_definition
from ..services.diff_service import Hunk, line_history
from ..services.editor_service import code_fence
from .context import context_scale
from .session import Session

# Total characters added to one reply's prompt, all routes together
# (~225 tokens). A block that doesn't fit whole is skipped, not truncated:
# half a definition misleads more than none.
_MAX_CHARS = 900
_MAX_DEFINITIONS = 2
_DEFINITION_CONTEXT_LINES = 3

_WHY_RE = re.compile(r"\b(why|reason|reasons|motivation|history|when\s+was)\b", re.IGNORECASE)
_BACKTICKED_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)(?:\(\))?`")
_CODE_LIKE_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+|[a-z]+[A-Z][A-Za-z0-9]*)\b|\b([A-Za-z_]\w*)\(\)"
)

HistoryLookup = Callable[[str, Hunk], list[str]]
DefinitionLookup = Callable[[str, str], Definition]


@dataclass(frozen=True)
class QuestionContext:
    text: str
    routes: tuple[str, ...]


def routes_for(question: str) -> list[str]:
    """Which lookups a question calls for, in the order they're added."""
    found = []
    if _WHY_RE.search(question):
        found.append("why")
    if _identifiers(question):
        found.append("definition")
    return found


def _identifiers(question: str) -> list[str]:
    """Names the reviewer is asking about: anything in backticks, plus words
    that look like code (snake_case, camelCase, or followed by "()")."""
    names = _BACKTICKED_RE.findall(question)
    for match in _CODE_LIKE_RE.finditer(question):
        names.append(match.group(1) or match.group(2))
    return list(dict.fromkeys(names))


def _mentions(text: str, name: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def question_context(
    session: Session,
    hunk: Hunk,
    question: str,
    history: list[dict],
    briefing_explains_why: bool,
    history_lookup: HistoryLookup = line_history,
    definition_lookup: DefinitionLookup = find_definition,
) -> QuestionContext | None:
    """The facts this question calls for, as one hidden prompt block, or
    None when no route fires or nothing was found."""
    # Same multiplier the opening narration's context uses — these caps were
    # measured against the local 8k window (see context_scale).
    scale = context_scale(session)
    blocks: list[tuple[str, str]] = []
    for route in routes_for(question):
        if route == "why" and not briefing_explains_why:
            commits = history_lookup(session.repo_path, hunk)
            if commits:
                blocks.append(
                    (
                        route,
                        "Recent commits that touched these lines (messages only):\n"
                        + "\n".join(f"- {c}" for c in commits),
                    )
                )
        elif route == "definition":
            for name in [n for n in _identifiers(question) if _mentions(hunk.diff_context, n)][
                : _MAX_DEFINITIONS * scale
            ]:
                block = _definition_block(session.repo_path, name, definition_lookup)
                if block:
                    blocks.append((route, block))

    kept: list[tuple[str, str]] = []
    used = 0
    for route, block in blocks:
        if used + len(block) > _MAX_CHARS * scale:
            continue
        kept.append((route, block))
        used += len(block)
    if not kept:
        return None
    return QuestionContext(
        text="Background looked up for this question (not part of the diff):\n\n" + "\n\n".join(b for _, b in kept),
        routes=tuple(dict.fromkeys(route for route, _ in kept)),
    )


def _definition_block(repo_path: str, name: str, lookup: DefinitionLookup) -> str | None:
    try:
        definition = lookup(repo_path, name)
    except DefinitionNotFound:
        return None
    offset = definition.line_number - definition.context_start
    lines = definition.lines[max(0, offset - 1) : offset + _DEFINITION_CONTEXT_LINES + 1]
    body = "\n".join(lines)
    fence = code_fence(body)
    return f"Where {name} is defined ({definition.file_path}:{definition.line_number}):\n{fence}\n{body}\n{fence}"
