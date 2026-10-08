"""Persists review progress to disk so it survives both a browser refresh
and a full app restart. A Session (app/web/session.py) lives only in
memory, for the lifetime of one WebSocket connection, so without this
everything it holds — reviewed marks, whether the review had started or
ended, queued comments — goes when the process does.

Follows the same on-disk-cache-under-repo_path convention already
established by briefing_service.py's ".briefing/" directory, and shares
".review/" with handle_finish_review's timestamped hand-off documents in
app/handlers/comments.py — this module's own file is a single fixed name
("session_state.json") in that same directory, continuously overwritten,
not one-per-action like those hand-off docs.

Reviewed status is keyed by stable_hunk_key (diff_service.py) — a hash of
the hunk's own content, not its positional index — so it survives a
completely fresh get_review_hunks() call across a restart as long as the
hunk's own lines are unchanged, regardless of what shifted around it.
narrations/conversation_histories/briefings are deliberately NOT persisted
here: briefings already have their own durable cache
(briefing_service.py), and re-narrating a revisited hunk after a restart
is an accepted one-time cost, not a correctness problem.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .diff_service import Hunk, stable_hunk_key

log = logging.getLogger(__name__)

_STATE_DIR = ".review"
_STATE_FILENAME = "session_state.json"

# What this app writes into whatever repo it is pointed at: this module's
# ".review", briefing_service.py's ".briefing", call_map.py/
# project_overview.py's ".context", and the apply-review skill a review plan
# can be saved as (review_handoff.write_skill), which is overwritten on
# every plan. Excluding them keeps them out of the reviewer's own
# `git status`, in a repo whose .gitignore has no reason to mention a tool
# its author may never have run, and out of the review itself, which lists
# untracked files that aren't ignored.
# The two debug directories are written relative to the reviewed repo too:
# briefing_service's prompt audit trail and editor_service's change requests.
_ARTIFACT_PATTERNS = (
    "/.review/",
    "/.briefing/",
    "/.briefing_debug/",
    "/.editor_debug/",
    "/.context/",
    "/.claude/skills/apply-review/",
)

# .git/info/exclude is the per-checkout, never-committed half of .gitignore
# — the right home for "this machine ran a tool here", since committing it
# would push a decision onto everyone else working in that repo.
_EXCLUDE_HEADER = "# Working files of AI Pear Review:"


def ensure_artifacts_ignored(repo_path: str) -> list[str]:
    """Adds this app's own directories to the repo's .git/info/exclude.

    Returns the patterns it added (empty if there was nothing to do), so a
    caller can say so once rather than every run.

    A linked worktree (a pull request checked out with "Checkout in
    Worktree") has .git as a file; its exclude file is the main
    repository's, which git names (see _exclude_file).

    Best-effort by design: a read-only .git, a .git file git can't follow,
    or no .git at all all mean "leave it alone and carry on". Failing to
    tidy someone's `git status` is never a reason to stop a review from
    starting.
    """
    exclude = _exclude_file(Path(repo_path))
    if exclude is None:
        return []
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        # Matched loosely ("/.review/", ".review/", ".review" and ".review/**"
        # all count), since the point is whether the reviewer has already
        # dealt with it, not whether they spelled it the way this app does.
        # Whole lines, so ".briefing_debug" doesn't count as ".briefing".
        covered = {_bare(line) for line in existing.splitlines()}
        missing = [p for p in _ARTIFACT_PATTERNS if _bare(p) not in covered]
        if not missing:
            return []
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        exclude.write_text(
            f"{existing}{prefix}\n{_EXCLUDE_HEADER}\n" + "".join(f"{p}\n" for p in missing),
            encoding="utf-8",
        )
        return missing
    except OSError as exc:
        log.info("Could not update %s: %s", exclude, exc)
        return []


def _bare(pattern: str) -> str:
    """A pattern without the slashes and globs that don't change what it names."""
    return pattern.strip().removesuffix("**").strip("/")


def _exclude_file(repo: Path) -> Path | None:
    """The repository's info/exclude: under .git, or, for a linked worktree
    (.git is a file), wherever git says it is — the main repository's,
    shared by all its worktrees. None when there's no usable git dir."""
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git / "info" / "exclude"
    if not dot_git.is_file():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else repo / path


@dataclass
class PersistedState:
    review_started: bool = False
    review_ended: bool = False
    reviewed_hunk_keys: list[str] = field(default_factory=list)
    pending_review_comments: list[dict] = field(default_factory=list)
    next_comment_id: int = 0


def _state_path(repo_path: str) -> Path:
    return Path(repo_path) / _STATE_DIR / _STATE_FILENAME


def load_persisted_state(repo_path: str) -> PersistedState | None:
    """None means "nothing to resume" — either a fresh repo (no file yet)
    or a file that couldn't be read/parsed. Persistence is a nice-to-have
    layered on top of a normal fresh Session, never worth failing the
    whole connection over (same philosophy as
    briefing_service.prune_briefing_cache's own error handling)."""
    path = _state_path(repo_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Could not read persisted review state at %s: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Persisted review state at %s is not valid JSON: %s", path, exc)
        return None
    return PersistedState(
        review_started=bool(data.get("review_started", False)),
        review_ended=bool(data.get("review_ended", False)),
        reviewed_hunk_keys=list(data.get("reviewed_hunk_keys", [])),
        pending_review_comments=list(data.get("pending_review_comments", [])),
        next_comment_id=int(data.get("next_comment_id", 0)),
    )


def save_persisted_state(repo_path: str, session) -> None:
    """Writes the session's review progress to disk.

    Synchronous on purpose: every call site is a small in-memory-to-JSON
    dump, not a slow git call, and handle_finish_review writes its own
    .review/ document the same way (app/handlers/comments.py).

    Never raises. A failed write must not break the interactive action that
    triggered it — marking a hunk reviewed, queuing a comment, and so
    on."""
    path = _state_path(repo_path)
    data = {
        "review_started": session.review_started,
        "review_ended": session.review_ended,
        "reviewed_hunk_keys": [stable_hunk_key(hunk) for i, hunk in enumerate(session.hunks) if i in session.reviewed],
        "pending_review_comments": session.pending_review_comments,
        "next_comment_id": session.next_comment_id,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save review state to %s: %s", path, exc)


def reconcile_reviewed(hunks: list[Hunk], reviewed_keys: list[str]) -> set[int]:
    """Maps persisted/remembered hunk keys back to *current* hunk indices —
    used both right after connect (against the on-disk state) and right
    after refresh_diff (against the in-memory reviewed set's own keys, so
    a refresh doesn't need its own disk write — the keys don't change
    shape, only which current index they now map to). A key with no
    matching hunk (its content changed, or it's gone) is silently dropped,
    not an error — that hunk is correctly unreviewed-again."""
    keys = set(reviewed_keys)
    return {i for i, hunk in enumerate(hunks) if stable_hunk_key(hunk) in keys}
