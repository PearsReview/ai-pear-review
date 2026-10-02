"""Working-tree diff reading and hunk splitting.

Reviews all changes to tracked files vs HEAD (staged + unstaged combined),
plus untracked files (git status "??", respecting .gitignore) — each shown
as a whole-file "all added" hunk, appended after the tracked-file hunks.
One hunk = one review step, binary files and rename-only diffs are skipped.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import subprocess
from dataclasses import dataclass, field

from .errors import ServiceError

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")

# This app's derived-cache directories: ".review" (session_store.py,
# settings_store.py), ".briefing" (briefing_service.py) and ".context"
# (call_map.py, project_overview.py). They are written into whatever
# repo_path points at, and the target repo's .gitignore has no reason to
# know about them, so git lists them as ordinary untracked files.
#
# Without this exclusion they surface as real work. Confirmed live: a repo
# reviewed twice showed more "hunks" on the second pass, none of them its
# own code, and a .briefing/*.json cache file was offered as a file to
# explore.
#
# Matched on the leading path segment only, so a project directory deeper
# in the tree that happens to share one of these names is unaffected.
_APP_ARTIFACT_DIRS = (".review", ".briefing", ".context")


def _is_app_artifact(path: str) -> bool:
    return path.replace("\\", "/").split("/", 1)[0] in _APP_ARTIFACT_DIRS


# A backstop, not a normal-case concern: a plain `git diff` or `ls-files`
# against a local working tree completes in well under a second. It exists
# for one documented failure mode — a `.git/index.lock` left behind by a
# concurrent git process (another terminal, an IDE's git integration, a
# second reviewer session) makes git block indefinitely on a lock that will
# never clear on its own.
#
# Without a timeout that hang is uncancellable, and for get_diff — called
# from the WebSocket connect path — it freezes the handshake with no
# recovery short of killing the server.
_GIT_TIMEOUT_SECONDS = 15


@dataclass
class Hunk:
    index: int
    file_path: str
    header: str  # the "@@ -a,b +c,d @@" line
    lines: list[str]  # the hunk body, including the header line
    # Whole-file context for the VS Code-style view: every line of the file's
    # diff against HEAD (kind: "context"|"add"|"del", old/new line numbers, text),
    # plus the [start, end] index range within it that *this* hunk covers, so
    # the UI can show the full file with just this hunk's lines highlighted.
    full_lines: list[dict] = field(default_factory=list)
    highlight_start: int = -1
    highlight_end: int = -1
    diff_context: str = field(init=False)

    def __post_init__(self) -> None:
        self.diff_context = "\n".join(self.lines)


def stable_hunk_key(hunk: Hunk) -> str:
    """Identifies this hunk by its own content, not its position — unlike
    hunk.index (reassigned sequentially by get_review_hunks on every call,
    so it drifts the moment an earlier hunk appears/disappears from the
    diff), this survives a full re-diff as long as the hunk's own lines
    haven't changed, regardless of where it now sits or what shifted
    around it. Used by session_store.py to persist "this hunk was marked
    reviewed" across restarts. Deliberately stronger than
    briefing_service.py's own cache key (file_path + hash(header)) — that
    one only needs to detect staleness, not survive as a lookup key across
    a completely fresh hunk list."""
    return f"{hunk.file_path}::{hashlib.sha256(hunk.diff_context.encode('utf-8')).hexdigest()}"


class DiffError(ServiceError):
    """Raised when the diff can't be read (not a repo, git missing, no commits yet, etc.)."""


def _run_git(repo_path: str, args: list[str], what: str, ok_codes: frozenset[int] = frozenset({0})) -> str:
    """Run one git command for this module and return its stdout, raising
    DiffError for every way it can fail.

    Two settings every caller needs, which is why they live here once:

    - errors="replace": a changed file in a legacy encoding (Latin-1,
      cp1252) is not valid UTF-8, and a strict decode fails inside
      subprocess's reader thread — stdout comes back as None and the
      caller dies with an AttributeError instead of a DiffError. One
      replacement character in the view is the right trade.
    - core.quotepath=off: by default git C-quotes any path with a
      non-ASCII byte ("caf\\303\\251.py"), which then matches nothing it
      is compared against.
    """
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=off", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise DiffError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        # Distinguished from a non-zero exit on purpose — this is not "git
        # ran and failed", it's "git never returned at all", almost always
        # because a concurrent git process (another terminal, an IDE's git
        # integration) is holding .git/index.lock. The message says so,
        # since that's the one actionable thing a reviewer can go check.
        raise DiffError(
            f"{what} timed out after {_GIT_TIMEOUT_SECONDS}s — likely a "
            "concurrent git process (e.g. a stale .git/index.lock) is blocking it"
        ) from exc

    if result.returncode not in ok_codes:
        raise DiffError(result.stderr.strip() or f"{what} failed")
    return result.stdout


def get_diff(repo_path: str = ".") -> str:
    return _run_git(repo_path, ["diff", "HEAD", "--no-color", "--unified=3"], "git diff HEAD")


def get_head_sha(repo_path: str = ".") -> str | None:
    """The current HEAD commit, or None if it can't be determined.

    Returns None rather than raising, unlike everything else here: the only
    caller is staleness *reporting* for the .context/ prep files, where not
    knowing the sha means "can't say whether it drifted" — a fine answer —
    and is never worth failing a request over. A fresh repo with no commits
    at all exits non-zero here and is one of the normal ways to get None.
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


_HISTORY_MARKER = "__commit__"


def line_history(repo_path: str, hunk: Hunk, limit: int = 3) -> list[str]:
    """ "<short sha> <date> <subject>" for the last few commits that touched
    the lines this hunk changes, newest first — or [] when there's nothing to
    ask about (a new file) or git can't answer.

    Uses the hunk's old-side range, since those are the lines with a history
    in HEAD. Returns [] rather than raising, like get_head_sha: this only
    ever adds optional context to a reply. The marker prefix keeps parsing
    independent of whether this git version honours -s with -L."""
    match = HUNK_HEADER_RE.match(hunk.header)
    if not match:
        return []
    start = int(match.group(1))
    count = int(match.group(2)) if match.group(2) is not None else 1
    if start == 0 or count == 0:
        return []  # nothing on the old side: a new file or a pure insertion point
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "log",
                f"-L{start},{start + count - 1}:{hunk.file_path}",
                f"--format={_HISTORY_MARKER}%h %as %s",
                "-s",
                "-n",
                str(limit),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line[len(_HISTORY_MARKER) :] for line in result.stdout.splitlines() if line.startswith(_HISTORY_MARKER)][
        :limit
    ]


def _diff_lines_from_process(
    repo_path: str, args: list[str], error_context: str, ok_codes: frozenset[int] = frozenset({0})
) -> list[dict]:
    """Shared by get_full_file_diff and get_untracked_file_diff: run a git
    diff variant with a huge --unified context (one file, one contiguous
    hunk covering the whole thing) and parse it into the full_lines shape.
    ok_codes differs between callers: plain `git diff HEAD` only ever
    exits non-zero on a real failure, but `git diff --no-index` (used for
    untracked files, since there's no HEAD entry to diff against) follows
    classic diff(1) exit codes — 1 means "differences found", the
    expected/normal case, not an error.
    """
    lines = _run_git(repo_path, args, error_context, ok_codes).splitlines()
    for i, line in enumerate(lines):
        match = HUNK_HEADER_RE.match(line)
        if match:
            old_start = int(match.group(1))
            new_start = int(match.group(3))
            return _assign_line_numbers(lines[i + 1 :], old_start, new_start)
    return []  # no hunk header at all — shouldn't happen for a non-binary, changed file


def get_full_file_diff(repo_path: str, file_path: str) -> list[dict]:
    """The whole file's diff against HEAD, as one contiguous list of lines.

    Uses a huge --unified context so git emits every line of the file in a
    single hunk instead of just a few lines around each change — this is
    what lets the UI show the full file (VS Code diff-view style) rather
    than an isolated snippet.
    """
    return _diff_lines_from_process(
        repo_path,
        ["diff", "HEAD", "--no-color", "--unified=1000000", "--", file_path],
        error_context=f"git diff HEAD for {file_path}",
    )


def get_untracked_files(repo_path: str = ".") -> list[str]:
    """Files git doesn't track at all yet (git status "??"), respecting
    .gitignore — `--exclude-standard` is what makes this list untracked-but-
    not-ignored files rather than everything git status would otherwise
    flag. Read-only: unlike `git add -N`, this never touches the index."""
    stdout = _run_git(repo_path, ["ls-files", "--others", "--exclude-standard"], "git ls-files")
    paths = (_unquote_git_path(line) for line in stdout.splitlines() if line)
    return [path for path in paths if not _is_app_artifact(path)]


def list_all_files(repo_path: str = ".") -> list[str]:
    """Every file in the working tree worth showing in an "explore" file
    browser — tracked files plus untracked-but-not-ignored ones (the same
    `--exclude-standard` gitignore handling get_untracked_files uses above),
    combined in one call rather than two. Unlike get_review_hunks, this has
    nothing to do with the diff at all — it's for browsing a file that has
    NOT changed, so a reviewer can ask about it for context before or
    alongside reviewing what has. Read-only, same subprocess/timeout/error
    handling as every other git call in this module."""
    stdout = _run_git(repo_path, ["ls-files", "--cached", "--others", "--exclude-standard"], "git ls-files")
    paths = (_unquote_git_path(line) for line in stdout.splitlines() if line)
    return [path for path in paths if not _is_app_artifact(path)]


def get_untracked_file_diff(repo_path: str, file_path: str) -> list[dict]:
    """An untracked file's content as a whole-file "all added" diff, in the
    same full_lines shape get_full_file_diff produces. Built via
    `git diff --no-index` against /dev/null (which git recognizes as "empty
    file" on Windows too, not just POSIX) rather than hand-rolling a
    synthetic line list, so it reuses the exact same header/line parsing as
    a real diff instead of a second path to keep in sync by hand."""
    return _diff_lines_from_process(
        repo_path,
        ["diff", "--no-index", "--no-color", "--unified=1000000", "--", "/dev/null", file_path],
        error_context=f"git diff --no-index for {file_path}",
        ok_codes=frozenset({0, 1}),
    )


def diff_text_to_full_lines(old_text: str, new_text: str) -> list[dict]:
    """Diffs two arbitrary text blobs (not git-based, no working-tree/HEAD
    involved) into the same full_lines shape get_full_file_diff/
    get_untracked_file_diff produce — used by app/handlers/act_now.py to preview
    a proposed change before anything is written to disk. Reuses
    difflib.unified_diff with a huge context window (n) so the whole file
    comes back as one contiguous hunk, same reasoning as get_full_file_diff's
    huge --unified value, then feeds it through the same header/line
    parsing as a real git diff rather than a second path to keep in sync.
    Returns [] if the two texts are identical (difflib produces no output
    at all in that case)."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    context = max(len(old_lines), len(new_lines), 1)
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=context))
    for i, line in enumerate(diff_lines):
        match = HUNK_HEADER_RE.match(line)
        if match:
            old_start = int(match.group(1))
            new_start = int(match.group(3))
            return _assign_line_numbers(diff_lines[i + 1 :], old_start, new_start)
    return []


def _assign_line_numbers(body_lines: list[str], old_start: int, new_start: int) -> list[dict]:
    """Walk a diff hunk's body (no header) tagging each line with old/new line numbers."""
    old_ln, new_ln = old_start, new_start
    result: list[dict] = []
    for line in body_lines:
        if line.startswith("\\ No newline"):
            continue  # git's "no newline at end of file" marker, not a real line
        prefix, text = (line[0], line[1:]) if line else (" ", "")
        if prefix == "+":
            kind = "add"
        elif prefix == "-":
            kind = "del"
        else:
            kind = "context"
        result.append(
            {
                "kind": kind,
                "old_lineno": None if kind == "add" else old_ln,
                "new_lineno": None if kind == "del" else new_ln,
                "text": text,
            }
        )
        if kind != "add":
            old_ln += 1
        if kind != "del":
            new_ln += 1
    return result


def _highlight_range(hunk_lines: list[str], full_lines: list[dict]) -> tuple[int, int]:
    """Locate this hunk's changed lines within the file's full_lines list.

    Matches by (kind, old_lineno, new_lineno) rather than position, since
    full_lines is the same diff at huge context — the changed lines carry
    identical line numbers in both, regardless of how hunks were split.
    """
    old_start, _, new_start, _ = _parse_hunk_header(hunk_lines[0])
    changed = [line for line in _assign_line_numbers(hunk_lines[1:], old_start, new_start) if line["kind"] != "context"]
    if not changed:
        return (-1, -1)
    wanted = {(line["kind"], line["old_lineno"], line["new_lineno"]) for line in changed}
    indices = [
        i
        for i, line in enumerate(full_lines)
        if line["kind"] != "context" and (line["kind"], line["old_lineno"], line["new_lineno"]) in wanted
    ]
    if not indices:
        return (-1, -1)
    return (min(indices), max(indices))


def _parse_hunk_header(header: str) -> tuple[int, int, int, int]:
    match = HUNK_HEADER_RE.match(header)
    if not match:
        return (1, 0, 1, 0)
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    return (old_start, old_count, new_start, new_count)


def _split_file_blocks(diff_text: str) -> list[list[str]]:
    """Split raw `git diff` output into per-file blocks (each starting at 'diff --git ...')."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current:
                blocks.append(current)
            current = [line]
        else:
            if current:
                current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _is_binary_block(block: list[str]) -> bool:
    return any(line.startswith("Binary files ") and line.endswith("differ") for line in block)


def _is_rename_only_block(block: list[str]) -> bool:
    """A rename/copy with no content changes has no '@@' hunk headers at all."""
    has_rename_marker = any(line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")) for line in block)
    has_hunks = any(HUNK_HEADER_RE.match(line) for line in block)
    return has_rename_marker and not has_hunks


_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _unquote_git_path(path: str) -> str:
    """Undo git's C-style quoting ("tab\\there.py" in quotes). core.quotepath=off
    stops it for non-ASCII names, but git still quotes a path containing a
    tab, newline, double quote or backslash. Octal escapes are raw UTF-8
    bytes, so the unescaped result is decoded as a whole."""
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        char = body[i]
        if char == "\\" and i + 1 < len(body):
            nxt, octal = body[i + 1], body[i + 1 : i + 4]
            if len(octal) == 3 and all(c in "01234567" for c in octal):
                out.append(int(octal, 8))
                i += 4
                continue
            if nxt in _C_ESCAPES:
                out.append(_C_ESCAPES[nxt])
                i += 2
                continue
        out.extend(char.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _header_path(line: str, prefix: str) -> str | None:
    """The path on a "--- a/..." or "+++ b/..." header line, or None for
    /dev/null (the missing side of an add or a delete)."""
    raw = _unquote_git_path(line[4:].rstrip("\t"))
    if raw == "/dev/null":
        return None
    return raw[len(prefix) :] if raw.startswith(prefix) else raw


def _file_path_from_block(block: list[str]) -> str:
    """The post-change path of one file's diff block (pre-change for a delete).

    Read from the "+++ b/" / "--- a/" header lines rather than "diff --git
    a/X b/Y": that line is ambiguous when a path itself contains " b/", and
    those header lines are unambiguous. Only lines before the first "@@"
    count — inside a hunk body, an added line that begins "++" also starts
    with "+++ ". A block with no content change (mode-only) has neither
    header line and falls back to the "diff --git" line."""
    old_path: str | None = None
    new_path: str | None = None
    for line in block[1:]:
        if HUNK_HEADER_RE.match(line):
            break
        if line.startswith("+++ "):
            new_path = _header_path(line, "b/")
        elif line.startswith("--- "):
            old_path = _header_path(line, "a/")
    path = new_path or old_path
    if path:
        return path
    match = _DIFF_GIT_RE.match(block[0])
    if match:
        return match.group(2) or match.group(1)
    return "unknown"


def _hunks_from_block(block: list[str], file_path: str, start_index: int) -> list[Hunk]:
    hunks: list[Hunk] = []
    current_lines: list[str] | None = None
    current_header = ""
    idx = start_index

    def flush() -> None:
        nonlocal current_lines, current_header, idx
        if current_lines is not None:
            hunks.append(Hunk(index=idx, file_path=file_path, header=current_header, lines=current_lines))
            idx += 1

    for line in block:
        if HUNK_HEADER_RE.match(line):
            flush()
            current_header = line
            current_lines = [line]
        elif current_lines is not None:
            current_lines.append(line)
    flush()
    return hunks


def _untracked_file_hunk(repo_path: str, file_path: str, index: int) -> Hunk | None:
    """One whole-file "all added" Hunk for an untracked file — every line is
    new by definition, so unlike a real hunk there's nothing to highlight
    within a larger file: the whole thing *is* the hunk. Returns None for
    an empty file (nothing to review). lines is reconstructed from
    full_lines rather than kept from the raw git output — safe because
    every full_lines entry here has kind == "add" by construction (see
    get_untracked_file_diff), so "+" + text is exactly what git's own raw
    diff body would have contained."""
    full_lines = get_untracked_file_diff(repo_path, file_path)
    if not full_lines:
        return None
    header = f"@@ -0,0 +1,{len(full_lines)} @@"
    return Hunk(
        index=index,
        file_path=file_path,
        header=header,
        lines=[header] + [f"+{line['text']}" for line in full_lines],
        full_lines=full_lines,
        highlight_start=0,
        highlight_end=len(full_lines) - 1,
    )


def get_review_hunks(repo_path: str = ".") -> list[Hunk]:
    """Read the working-tree diff against HEAD, plus untracked files, and
    return one Hunk per review step, in file order (untracked files appended
    after the tracked-file hunks — simpler than interleaving, and the file
    sidebar already lets a reviewer jump straight to any file regardless of
    list order).

    Skips binary files and rename/copy-only blocks (no content change) per v1 scope.
    Each hunk also carries the full file's diff (see Hunk.full_lines) so the UI
    can render the whole file with just this hunk's lines highlighted.
    """
    diff_text = get_diff(repo_path)
    hunks: list[Hunk] = []
    full_lines_cache: dict[str, list[dict]] = {}
    for block in _split_file_blocks(diff_text):
        if _is_binary_block(block) or _is_rename_only_block(block):
            continue
        file_path = _file_path_from_block(block)
        file_hunks = _hunks_from_block(block, file_path, start_index=len(hunks))

        if file_path not in full_lines_cache:
            full_lines_cache[file_path] = get_full_file_diff(repo_path, file_path)
        full_lines = full_lines_cache[file_path]

        for hunk in file_hunks:
            hunk.full_lines = full_lines
            hunk.highlight_start, hunk.highlight_end = _highlight_range(hunk.lines, full_lines)

        hunks.extend(file_hunks)

    for file_path in get_untracked_files(repo_path):
        untracked = _untracked_file_hunk(repo_path, file_path, index=len(hunks))
        if untracked is not None:
            hunks.append(untracked)

    return hunks
