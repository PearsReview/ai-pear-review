"""The Finish Review hand-off: what a queued review comment records about
its code, and the plan built from the whole queue.

A comment keeps a reference to its code rather than a copy of whole files:
the diff lines it covers (with their add/del/context kind), a few lines
either side, the hunk header and line numbers. That is enough for an agent
to find the spot again, and small enough that a review of large files
still fits comfortably in the agent's context. The agent reads the files
themselves, at their current state, when it acts.

build_review turns the queue into a structured review, held in memory
only; render_plan renders that as instructions and a checklist for the
reviewer's coding agent, with no model call. The plan is the only file
written — it carries everything the agent needs. At Finish Review each
comment is checked against the file as it is now (check_status), so the
agent is told which comments point at code that has since moved or
changed.

Pure functions of hunks, comment dicts and the files on disk — nothing
here talks to a socket, a Session or a model."""

from __future__ import annotations

from pathlib import Path

from .diff_service import Hunk
from .editor_service import code_fence, resolve_within_repo

CONTEXT_LINES = 3
SEVERITY_ORDER = ("must-fix", "suggestion", "nit")
_SEVERITY_HEADINGS = {"must-fix": "Must fix", "suggestion": "Suggestions", "nit": "Nits"}
_DIFF_PREFIX = {"add": "+", "del": "-", "context": " "}

# check_status's answers. "not_checked": the comment covers only removed
# lines, which by definition are no longer in the working tree to look for.
STATUS_OK = "ok"
STATUS_MOVED = "moved"
STATUS_CHANGED = "changed"
STATUS_FILE_DELETED = "file_deleted"
STATUS_NOT_CHECKED = "not_checked"
_STATUS_NOTES = {
    STATUS_MOVED: "the code has moved since this comment was written",
    STATUS_CHANGED: "the code this comment points at has changed or is gone — confirm it still applies",
    STATUS_FILE_DELETED: "this file no longer exists",
    STATUS_NOT_CHECKED: "covers removed lines only, so it could not be checked against the current file",
}


def _line(entry: dict) -> dict:
    return {"kind": entry.get("kind", "context"), "text": entry.get("text", "")}


def _find_index(full_lines: list[dict], old_lineno: int | None, new_lineno: int | None) -> int:
    return next(
        (
            i
            for i, line in enumerate(full_lines)
            if line["old_lineno"] == old_lineno and line["new_lineno"] == new_lineno
        ),
        -1,
    )


def _span(numbers: list[dict], key: str) -> tuple[int | None, int | None]:
    values = [n[key] for n in numbers if n.get(key) is not None]
    return (values[0], values[-1]) if values else (None, None)


def comment_location(hunks: list[Hunk], hunk: Hunk, marked_lines: list[dict] | None) -> dict:
    """What a queued comment records about its code: {"hunk_header",
    "lines": {old_start, old_end, new_start, new_end}, "code",
    "context_before", "context_after"}, where each code/context entry is
    {"kind": "add"|"del"|"context", "text"}.

    Marked lines are looked up in their file's full_lines (shared by every
    hunk of that file), which is the authority on each line's kind and
    what surrounds it. Without marks the comment covers `hunk`'s changed
    lines. A marked line that can't be found there (a stale client view)
    keeps the text and kind the client sent, with no context."""
    if marked_lines:
        file_path = marked_lines[0].get("file_path", hunk.file_path)
        file_hunks = [h for h in hunks if h.file_path == file_path] or [hunk]
        full_lines = file_hunks[0].full_lines
        first = _find_index(full_lines, marked_lines[0].get("old_lineno"), marked_lines[0].get("new_lineno"))
        last = _find_index(full_lines, marked_lines[-1].get("old_lineno"), marked_lines[-1].get("new_lineno"))
        if first == -1 or last == -1:
            code = [_line(m) for m in marked_lines]
            return _location(None, code, marked_lines, [], [])
        lo, hi = min(first, last), max(first, last)
        header = next((h.header for h in file_hunks if h.highlight_start <= lo <= h.highlight_end), None)
    else:
        full_lines = hunk.full_lines
        lo, hi = hunk.highlight_start, hunk.highlight_end
        header = hunk.header
        if lo < 0 or hi >= len(full_lines):
            code = [
                {"kind": "add" if raw[:1] == "+" else "del" if raw[:1] == "-" else "context", "text": raw[1:]}
                for raw in hunk.lines[1:]
                if not raw.startswith("\\")
            ]
            return _location(header, code, [], [], [])

    selected = full_lines[lo : hi + 1]
    return _location(
        header,
        [_line(line) for line in selected],
        selected,
        [_line(line) for line in full_lines[max(0, lo - CONTEXT_LINES) : lo]],
        [_line(line) for line in full_lines[hi + 1 : hi + 1 + CONTEXT_LINES]],
    )


def _location(header: str | None, code: list[dict], numbers: list[dict], before: list[dict], after: list[dict]) -> dict:
    old_start, old_end = _span(numbers, "old_lineno")
    new_start, new_end = _span(numbers, "new_lineno")
    return {
        "hunk_header": header,
        "lines": {"old_start": old_start, "old_end": old_end, "new_start": new_start, "new_end": new_end},
        "code": code,
        "context_before": before,
        "context_after": after,
    }


def check_status(repo_path: str, comment: dict) -> tuple[str, dict | None]:
    """Whether a comment's code is still where it was: (status, the lines it
    is at now when moved, else None).

    Compares the comment's added and unchanged lines, which are the ones
    that exist in the working tree, against the file on disk. A comment
    saved before "code" was recorded has nothing to compare and is
    not_checked."""
    code = comment.get("code")
    if not code:
        return STATUS_NOT_CHECKED, None
    target = [line["text"] for line in code if line["kind"] != "del"]
    if not target:
        return STATUS_NOT_CHECKED, None
    path = resolve_within_repo(repo_path, comment["file_path"])
    if path is None or not path.is_file():
        return STATUS_FILE_DELETED, None
    try:
        current = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return STATUS_FILE_DELETED, None

    expected = (comment.get("lines") or {}).get("new_start")
    if expected is not None and current[expected - 1 : expected - 1 + len(target)] == target:
        return STATUS_OK, None
    starts = [i for i in range(len(current) - len(target) + 1) if current[i : i + len(target)] == target]
    if not starts:
        return STATUS_CHANGED, None
    nearest = min(starts, key=lambda i: abs(i + 1 - (expected or 1)))
    return STATUS_MOVED, {"new_start": nearest + 1, "new_end": nearest + len(target)}


_REVIEW_FIELDS = (
    "id",
    "severity",
    "instruction",
    "file_path",
    "where",
    "hunk_header",
    "lines",
    "code",
    "context_before",
    "context_after",
    "created_at",
)


def build_review(
    comments: list[dict], overall_note: str, repo_path: str, base_commit: str | None, finished_at: str
) -> dict:
    """The review render_plan works from: the queue in severity order, each
    comment checked against the current files. The UI-only fields (anchor,
    snippet) are left out."""
    rank = {severity: i for i, severity in enumerate(SEVERITY_ORDER)}
    ordered = sorted(comments, key=lambda c: rank.get(c.get("severity", ""), len(rank)))
    entries = []
    for c in ordered:
        entry = {key: c.get(key) for key in _REVIEW_FIELDS}
        if not entry["code"]:
            # Queued before code was recorded: keep what there is.
            entry["snippet"] = c.get("snippet", "")
        status, now_at = check_status(repo_path, c)
        entry["status"] = status
        if now_at is not None:
            entry["current_lines"] = now_at
        entries.append(entry)
    return {
        "repo": Path(repo_path).resolve().name,
        "base_commit": base_commit,
        "finished_at": finished_at,
        "overall_note": overall_note or None,
        "comments": entries,
    }


_INSTRUCTIONS = """\
## How to use this plan

Each item below is a reviewer's comment on specific lines of the uncommitted
changes in this repository.

1. Read each item and the code it points at. Line numbers are from when
   the comment was written; if they have shifted, use the quoted code and
   its surrounding lines to find the spot.
2. Items flagged **stale** point at code that moved or changed after the
   comment was written. Confirm the comment still applies before acting on
   it, and skip it with a note if it no longer does.
3. Before editing anything, present a short plan: what you will change for
   each item and in what order, noting any items that conflict with or
   depend on each other. Wait for approval.
4. Apply must-fix items first. Change only what the comments ask for.
5. Finish with the Verify checklist, and report which items you addressed
   and which you skipped, and why."""

_VERIFY = """\
## Verify

- [ ] Every item above is addressed, or skipped with a stated reason
- [ ] The project's tests pass
- [ ] `git diff` shows no changes beyond what the comments asked for"""


def _diff_block(lines: list[dict]) -> str:
    body = "\n".join(_DIFF_PREFIX.get(line["kind"], " ") + line["text"] for line in lines)
    fence = code_fence(body)
    return f"{fence}diff\n{body}\n{fence}"


def _excerpt(entry: dict) -> str:
    """The commented lines on their own, so it is clear what the comment is
    about, then the lines around them — there only to find the spot."""
    if not entry.get("code"):
        text = entry.get("snippet", "")
        fence = code_fence(text)
        return f"{fence}\n{text}\n{fence}"
    before, after = entry.get("context_before") or [], entry.get("context_after") or []
    parts = [_diff_block(entry["code"])]
    if before or after:
        gap = [{"kind": "context", "text": "…"}]
        parts.append("Surrounding lines, to help find the spot:\n\n" + _diff_block([*before, *gap, *after]))
    return "\n\n".join(parts)


def _line_range(start: int, end: int) -> str:
    return f"line {start}" if start == end else f"lines {start}-{end}"


def _item(number: int, entry: dict) -> str:
    title = f"### {number}. {entry['file_path']} — {entry.get('where') or 'whole hunk'}"
    parts = [title, "- [ ] Done"]
    status = entry.get("status")
    if status in _STATUS_NOTES:
        note = _STATUS_NOTES[status]
        if status == STATUS_MOVED and entry.get("current_lines"):
            now = entry["current_lines"]
            note += f" (now at {_line_range(now['new_start'], now['new_end'])})"
        parts.append(f"**Stale:** {note}." if status != STATUS_NOT_CHECKED else f"_Note: {note}._")
    parts.append("\n".join(f"> {line}" if line else ">" for line in entry["instruction"].splitlines()))
    parts.append(_excerpt(entry))
    if entry.get("hunk_header"):
        # Plain markdown, not <sub>: the in-app preview never renders HTML.
        parts.append(f"Hunk: `{entry['hunk_header']}`")
    return "\n\n".join(parts)


def render_plan(review: dict) -> str:
    """The hand-off plan: instructions for the reviewer's coding agent, then
    every comment grouped by severity, then a verify checklist."""
    commit = review.get("base_commit")
    header = [f"# Review plan — {review['repo']}"]
    meta = f"Review finished {review['finished_at']}"
    if commit:
        meta += f", against commit `{commit[:12]}` plus uncommitted changes"
    header.append(meta + ".")
    parts = ["\n\n".join(header)]
    if review.get("overall_note"):
        parts.append("## Reviewer's note\n\n" + review["overall_note"])
    parts.append(_INSTRUCTIONS)

    number = 0
    for severity in (*SEVERITY_ORDER, None):
        group = [
            c
            for c in review["comments"]
            if (c.get("severity") == severity if severity else c.get("severity") not in SEVERITY_ORDER)
        ]
        if not group:
            continue
        section = [f"## {_SEVERITY_HEADINGS.get(severity or '', 'Other')}"]
        for entry in group:
            number += 1
            section.append(_item(number, entry))
        parts.append("\n\n".join(section))

    parts.append(_VERIFY)
    return "\n\n".join(parts) + "\n"


PLANS_DIR = ".review"
PLAN_PREFIX = "review_"


def latest_review_plan(repo_path: str) -> str | None:
    """The newest hand-off plan as a repo-relative posix path, or None.

    Read from disk rather than remembered, so it survives a reload or a
    restart. Plans are named review_<YYYYmmdd_HHMMSS>[_n].md, which sorts
    by time as text ("." sorts before "_", so a same-second _2 comes
    after its first)."""
    plans = sorted((Path(repo_path) / PLANS_DIR).glob(f"{PLAN_PREFIX}*.md"))
    return f"{PLANS_DIR}/{plans[-1].name}" if plans else None


# The optional agent-skill copy of the plan. Claude Code and Cline both
# discover skills under .claude/skills/, so one copy serves either agent:
# the reviewer runs /apply-review, or just asks to apply the review.
SKILL_NAME = "apply-review"
SKILL_DIR = Path(".claude") / "skills" / SKILL_NAME
# How this app recognises a skill it wrote, and so may overwrite. A file at
# the same path without it is the user's own and is never touched.
SKILL_MARKER = "<!-- Written by AI Pear Review; replaced each time a review plan is created. -->"


def render_skill(plan_md: str, finished_at: str) -> str:
    """The plan as a SKILL.md: frontmatter both agents read, the marker,
    then the plan itself inline, so the skill stands on its own.

    The description is deliberately narrow. Agents read every skill's
    description to decide when to use it, and a vague one would pull the
    plan into unrelated tasks."""
    description = (
        f"Apply the code-review plan AI Pear Review wrote at {finished_at}. "
        "Use only when the user asks to apply the review or the review plan."
    )
    return (
        f"---\nname: {SKILL_NAME}\ndescription: {description}\n---\n\n{SKILL_MARKER}\n\n"
        'Follow the review plan below exactly, starting with its "How to use this plan" section.\n\n'
        f"{plan_md}"
    )


def write_skill(repo_path: str, text: str) -> str | None:
    """Writes .claude/skills/apply-review/SKILL.md. Returns None when
    written, or a sentence saying why not — never raises, since the plan
    file itself is already saved by the time this runs."""
    path = Path(repo_path) / SKILL_DIR / "SKILL.md"
    try:
        if path.exists() and SKILL_MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            return f"{SKILL_DIR.as_posix()} already exists and wasn't written by this app, so it was left as it is."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return f"Couldn't save the plan as a skill: {exc}"
    return None
