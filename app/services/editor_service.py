"""Builds the text a reviewer pastes into their own interactive Claude Code
session to apply a discussed change.

This app never calls a model to make an edit and never writes to a file.
The edit itself is made by a session the reviewer already trusts and
controls directly.
"""

from __future__ import annotations

from pathlib import Path

from ..utils.debug_files import write_debug_prompt
from .diff_service import HUNK_HEADER_RE, Hunk

_DEBUG_DIR = ".editor_debug"

_REQUEST_PREAMBLE = """\
A code reviewer and the author of this change discussed a hunk in this
file (shown below, in the context of the full current file) and agreed on
one specific edit. Make the change that was actually discussed — it may be
within the highlighted hunk itself, or somewhere else in the file the
reviewer pointed out while looking at it. Don't invent changes nobody
raised. If anything here is ambiguous, ask before guessing.
"""


def resolve_within_repo(repo_path: str, file_path: str) -> Path | None:
    """Resolves file_path against repo_path and refuses to leave it —
    file_path can originate from client JSON (see the handlers' marked_lines
    handling), so a bare os.path.join here would let "../../.ssh/id_rsa"
    or an absolute path escape the repo entirely. Returns None on escape,
    the same "caller treats this as unavailable" contract read_current_file
    already had for a missing file, so every caller inherits the guard for
    free rather than needing its own check."""
    resolved_repo = Path(repo_path).resolve()
    candidate = (resolved_repo / file_path).resolve()
    if not candidate.is_relative_to(resolved_repo):
        return None
    return candidate


def read_current_file(repo_path: str, file_path: str) -> str | None:
    """Best-effort: a missing/unreadable/binary file, or one that would
    resolve outside repo_path, just means the caller falls back to
    hunk-only context rather than failing the whole request over it.
    Public (not module-private) — also reused by app/handlers/comments.py's batched-review
    document builder, not just build_change_request below."""
    safe_path = resolve_within_repo(repo_path, file_path)
    if safe_path is None:
        return None
    try:
        with open(safe_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def code_fence(content: str) -> str:
    """A backtick fence one character longer than the longest run of
    backticks already in content — the CommonMark rule for fencing text
    that might itself contain fenced code blocks. Reviewing a markdown
    file that has its own ``` block would otherwise break the structure of
    every generated document that wraps file content in a hardcoded triple
    fence (this function's callers, plus app/handlers/comments.py's review-document
    builder)."""
    longest_run = 0
    current_run = 0
    for ch in content:
        if ch == "`":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    return "`" * max(3, longest_run + 1)


def hunk_line_range(header: str) -> str:
    """New-file line range this hunk spans, e.g. "lines 4-9" — parsed from
    the "@@ -a,b +c,d @@" header diff_service.py already produces. Cosmetic
    orientation only; falls back to empty on a parse miss rather than
    failing the request."""
    match = HUNK_HEADER_RE.match(header)
    if not match:
        return ""
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    if new_count <= 1:
        return f"line {new_start}"
    return f"lines {new_start}-{new_start + new_count - 1}"


def build_change_request(hunk: Hunk, discussion: str, repo_path: str = ".") -> str:
    """Assembles the pasteable request text (preamble + full current file
    content, if readable + the hunk's diff + the discussion transcript) and
    persists a debug copy as a side effect (see write_debug_prompt). Never
    raises over a missing/unreadable file — read_current_file's
    best-effort contract just means the request goes out with hunk-only
    context instead of the full file."""
    file_content = read_current_file(repo_path, hunk.file_path)
    line_range = hunk_line_range(hunk.header)
    where = f" ({line_range} in the current file)" if line_range else ""

    parts = [_REQUEST_PREAMBLE, "", f"File: {hunk.file_path}", ""]
    if file_content is not None:
        fence = code_fence(file_content)
        parts.append(f"Full current file content:\n{fence}\n{file_content}\n{fence}\n")
    diff_fence = code_fence(hunk.diff_context)
    parts.append(f"The hunk currently under review{where}:\n{diff_fence}\n{hunk.diff_context}\n{diff_fence}\n")
    parts.append(f"Recent discussion:\n{discussion}")
    text = "\n".join(parts)

    # Persistent record of every request generated, same debug-dir pattern
    # briefing_service.py's stub mode already uses — also a fallback if the
    # browser's clipboard write ever silently fails.
    write_debug_prompt(_DEBUG_DIR, f"hunk_{hunk.index}.txt", text)
    return text
