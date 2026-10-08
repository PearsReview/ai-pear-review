"""Background facts looked up for one reviewer question, chosen by what
the question asks — rather than injected into every prompt.

A small model on CPU pays for every token in the window, and a narration
already carries project context, the call map and the briefing. So the
facts below are fetched only when a reply's question makes them relevant:

    (see the _*_RE patterns below for the routes)

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

from ..services.call_map import call_map_prompt_block, coverage_prompt_block
from ..services.code_search import Definition, DefinitionNotFound, find_definition
from ..services.diff_service import Hunk, line_history
from ..services.editor_service import code_fence
from .context import context_scale
from .session import Session

# Total characters added to one reply's prompt, all routes together
# (~225 tokens). A block that doesn't fit whole is skipped, not truncated:
# half a definition or half a caller list misleads more than none.
_MAX_CHARS = 1200
_MAX_DEFINITIONS = 2
_DEFINITION_CONTEXT_LINES = 3
# [TEST] added block for the PR review scratch test.
_MAX_CALLERS = 5
_MAX_TESTS = 3
_MAX_HISTORY = 4

_TESTS_RE = re.compile(r"\b(tests?|tested|testing|coverage|covered)\b", re.IGNORECASE)
_CALLERS_RE = re.compile(
    r"\b(callers?|called\s+(?:by|from)|who\s+(?:calls|uses)|used\s+by|depends?\s+on|dependents?|"
    r"break|breaks|impact|affects?)\b",
    re.IGNORECASE,
)
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
    if _TESTS_RE.search(question):
        found.append("tests")
    if _CALLERS_RE.search(question):
        found.append("callers")
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
        if route == "tests":
            block = coverage_prompt_block(session.repo_path, hunk.file_path, hunk.diff_context)
            if block:
                blocks.append((route, block))
        elif route == "callers":
            block = call_map_prompt_block(session.repo_path, hunk.file_path, hunk.diff_context, scale)
            # Narration's opening prompt already carries this; don't send it twice.
            if block and not any(block in (m.get("content") or "") for m in history):
                blocks.append((route, block))
        elif route == "why" and not briefing_explains_why:
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
