""" "Step into" a highlighted identifier — find where it's declared/defined.

Deliberately not an LLM call: this backs an interactive click (select a name
in the code view, jump to its definition), so it needs to be fast and free,
not pay agentic-CLI latency. `git grep` against a handful of common
definition patterns (Python def/class, JS function/const/class) covers the
common cases; anything it can't find comes back as a clear "not found"
rather than a guess. This is the fast path only; deeper, agent-driven
lookups are out of scope here.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from .errors import ServiceError

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

# A local `git grep` normally returns near-instantly, even against a large
# tree — this exists as a backstop against the same real-world failure mode
# diff_service.py guards against with its own timeout constant: a
# concurrent git process (another terminal, an IDE's git integration)
# holding .git/index.lock makes git block indefinitely rather than fail
# fast. Without this, a "Step Into" click during that window would hang
# uncancellably instead of coming back as a normal "not found"-shaped error.
_GIT_GREP_TIMEOUT_SECONDS = 10

# Checked in order; first pattern with any match wins. {name} is the
# re.escape()'d identifier. Deliberately simple/heuristic, same caveat as
# briefing_service.py's call-map idea: good enough to jump somewhere useful,
# not a real parser — will miss dynamic definitions, decorators-that-rename,
# and same-named symbols in multiple languages/files (first match wins).
_DEFINITION_PATTERNS = [
    r"^\s*(async\s+)?def\s+{name}\s*\(",  # Python function/method
    r"^\s*class\s+{name}\b",  # Python/JS class
    r"^\s*(export\s+)?(async\s+)?function\s+{name}\s*\(",  # JS function
    r"^\s*(export\s+)?(const|let|var)\s+{name}\s*=",  # JS var/const/arrow fn
    r"^\s*{name}\s*[:=]\s*(async\s*)?\(",  # object method / arrow shorthand
]


class DefinitionNotFound(ServiceError):
    """Raised when no definition-shaped match is found for the identifier —
    also the domain error this module raises for a git-grep timeout or an
    unreadable match file, since to a caller those are the same "couldn't
    resolve it" outcome as a genuine no-match."""


@dataclass
class Definition:
    file_path: str
    line_number: int  # 1-indexed, the matched definition line
    lines: list[str]  # a window of context lines around the match
    context_start: int  # 1-indexed line number of lines[0]


def find_definition(repo_path: str, identifier: str, context: int = 6) -> Definition:
    """Tries each pattern in _DEFINITION_PATTERNS in order against a `git
    grep`, returning a context window around the first match found anywhere
    in the repo. Raises DefinitionNotFound — never a bare exception — for
    every failure mode: not a single identifier, no pattern matched
    anywhere, a git-grep timeout, or the matched file being unreadable by
    the time this reads it back."""
    identifier = identifier.strip()
    if not _IDENTIFIER_RE.match(identifier):
        raise DefinitionNotFound(
            f"'{identifier}' doesn't look like a single identifier — select just a function, class, or variable name."
        )

    for pattern in _DEFINITION_PATTERNS:
        regex = pattern.format(name=re.escape(identifier))
        try:
            result = subprocess.run(
                # --untracked: by default git grep only searches tracked
                # content, which would miss a definition living in a file the
                # reviewer hasn't git-added yet (see diff_service.py's
                # get_untracked_files) — still respects .gitignore.
                # errors="replace" and core.quotepath=off for the same reasons
                # as diff_service._run_git: a match in a Latin-1 file, or in a
                # file with a non-ASCII name.
                ["git", "-c", "core.quotepath=off", "-C", repo_path, "grep", "--untracked", "-n", "-E", regex],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_GIT_GREP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            # Raised immediately rather than trying the next pattern: if
            # git itself is stuck — almost certainly a concurrent process
            # holding .git/index.lock, the same cause diff_service.py
            # documents — every later `git grep` in this loop would hang
            # the same way.
            #
            # Raised as DefinitionNotFound, this module's one domain error,
            # worded to separate "git timed out" from the ordinary "no
            # match" case below, so it doesn't read as though the
            # identifier simply isn't defined.
            raise DefinitionNotFound(
                f"git grep timed out after {_GIT_GREP_TIMEOUT_SECONDS}s while looking for "
                f"'{identifier}' — likely a concurrent git process (e.g. a stale "
                ".git/index.lock) is blocking it"
            ) from exc
        if result.returncode == 0 and result.stdout.strip():
            first_match = result.stdout.splitlines()[0]
            try:
                file_path, line_str, _ = first_match.split(":", 2)
                line_number = int(line_str)
            except ValueError as exc:
                # git grep -n's own "file:line:text" format shouldn't
                # produce this, but a path containing a literal ":" would —
                # same "couldn't resolve it" outcome as any other failure
                # here, not an unhandled crash over an unparsed grep line.
                raise DefinitionNotFound(
                    f"could not parse git grep match line for '{identifier}': {first_match!r}"
                ) from exc
            return _load_context(repo_path, file_path, line_number, context)

    raise DefinitionNotFound(f"No definition found for '{identifier}'.")


def _load_context(repo_path: str, file_path: str, line_number: int, context: int) -> Definition:
    full_path = os.path.join(repo_path, file_path)
    try:
        with open(full_path, encoding="utf-8", errors="replace") as f:
            all_lines = f.read().splitlines()
    except OSError as exc:
        # git grep found a matching line, but the file is gone/unreadable
        # by the time we get here (deleted or permissions changed between
        # the two calls) — same "couldn't resolve it" outcome to the
        # caller as a genuine no-match, not an unhandled crash.
        raise DefinitionNotFound(f"'{file_path}' matched but could not be read: {exc}") from exc

    start = max(0, line_number - 1 - context)
    end = min(len(all_lines), line_number - 1 + context + 1)
    return Definition(
        file_path=file_path,
        line_number=line_number,
        lines=all_lines[start:end],
        context_start=start + 1,
    )
