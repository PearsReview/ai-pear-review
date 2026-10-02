"""Reads the repo-level overview a prep skill left behind, and slices it
down to something small enough to put in a prompt.

The conversation model is handed one hunk's diff and nothing else — not
the file it lives in, not the other hunks, and certainly not any sense of
what the project *is*. That gap is why it will explain a two-line change
in terms of a subsystem the repo doesn't have. Nothing in the app can fill
it: investigating a codebase needs tools and a capable model, which is
exactly what the .claude/skills/project-overview skill has and this app
deliberately doesn't (see briefing_service.py's module docstring on why
the app never drives a CLI itself).

Stored under .context/ rather than .briefing/ for one concrete reason:
prune_briefing_cache() deletes .briefing/ entries that no longer match
the diff on every Refresh Diff, which is correct for per-hunk briefings
tied to exact diff content and completely wrong for an architectural overview that took real
work to produce and doesn't go stale when a diff shifts by a line.

Staleness is treated differently here than for briefings, on purpose. A
briefing is bound to one hunk's exact bytes, so a mismatch means "this
describes code that no longer exists" and it's dropped outright. An
overview describes the shape of a project, which does not stop being true
because someone edited a function — so a HEAD change is recorded and
reported, not grounds for throwing it away. What would make it dangerous
is age nobody can see, which is what head_sha/generated_at exist for.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CONTEXT_DIR = ".context"
_OVERVIEW_FILENAME = "project_overview.json"

# Hard ceiling on what gets injected, in characters. The conversation model
# runs on a few thousand tokens total (see config.yaml's num_ctx note) and
# prompt cost is linear, so an overview that crowds out the diff makes
# narration worse, not better — the diff is the thing actually being
# reviewed. ~4 chars/token, so this is roughly 250 tokens for the digest
# and 100 for the per-file note.
_MAX_DIGEST_CHARS = 1000
_MAX_COMPONENT_CHARS = 400

# Conventions are capped harder than the rest, because unlike the digest
# and the file role they aren't about the code being reviewed — they're
# standing rules that apply to every hunk equally. That makes them the
# first thing worth cutting when budget is tight, so only the few the
# skill ranked highest get through.
_MAX_CONVENTIONS = 3
_MAX_CONVENTION_CHARS = 160


def overview_path(repo_path: str) -> Path:
    return Path(repo_path) / _CONTEXT_DIR / _OVERVIEW_FILENAME


def load_overview(repo_path: str) -> dict | None:
    """The stored overview, or None if there isn't one / it's unreadable.
    Absence is the normal case — the skill is optional, and everything
    downstream treats a missing overview exactly like the behaviour before
    overviews existed."""
    path = overview_path(repo_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.info("Ignoring unreadable project overview at %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not data.get("digest"):
        return None  # a skeleton with nothing filled in is not usable
    return data


def overview_prompt_block(repo_path: str, file_path: str | None = None, scale: int = 1) -> str | None:
    """The part of the overview worth spending prompt budget on: the
    project digest, plus — when the hunk's own file has an entry — that
    file's one-line role.

    Sliced rather than injected whole. A full overview is a document; what
    a single narration needs is "what is this project" and "what is this
    one file for", which is a couple of sentences. Everything else in the
    file is there for a human reading .context/project_overview.md.

    Both parts are truncated defensively: this is skill-written text the
    app doesn't control, and one over-long entry silently eating the
    budget would degrade every narration in a way that's very hard to
    trace back here.

    scale multiplies those caps for a session whose provider manages its
    own window — see context_scale in app/web/context.py. The caps here
    were measured against an 8k local window, where they are a real
    fraction of the budget; against a 1M-token window they throw away
    context for nothing."""
    overview = load_overview(repo_path)
    if overview is None:
        return None

    parts = [f"About this project: {overview['digest'][: _MAX_DIGEST_CHARS * scale].strip()}"]

    if file_path:
        role = _component_role(overview, file_path)
        if role:
            parts.append(f"About {file_path}: {role[: _MAX_COMPONENT_CHARS * scale].strip()}")

    conventions = _conventions(overview, scale)
    if conventions:
        # Worth the tokens because it's the one part of the overview a
        # reviewer can check a diff *against* — a change that breaks a
        # house rule is a finding, where the digest and file role are only
        # ever framing.
        parts.append("House rules in this project: " + " ".join(conventions))

    parts.append("That background is context, not something to describe — the reviewer knows their own project.")
    return "\n".join(parts)


def _conventions(overview: dict, scale: int = 1) -> list[str]:
    """The first few recorded conventions, whole. Order is the skill's —
    it's told to put the most load-bearing one first — so this takes from
    the front rather than trying to rank them itself.

    A convention that doesn't fit the cap is dropped, not truncated.
    Cutting to the character and adding a full stop produces sentences like
    "a helper two handlers need moves down into." — text that reads as
    complete and states something false. Same rule question_context.py
    applies to its own blocks: half a rule misleads more than no rule."""
    conventions = overview.get("conventions")
    if not isinstance(conventions, list):
        return []
    limit = _MAX_CONVENTION_CHARS * scale
    out = []
    for convention in conventions[:_MAX_CONVENTIONS]:
        if not isinstance(convention, str) or not convention.strip():
            continue
        text = convention.strip()
        if len(text) > limit:
            continue
        out.append(text if text.endswith(".") else text + ".")
    return out


def _component_role(overview: dict, file_path: str) -> str | None:
    """This file's recorded role, matched exactly first and then by longest
    matching path prefix — so an entry for "app/services/" still covers
    app/services/whatever.py without the skill having to enumerate every
    file in the repo."""
    components = overview.get("components")
    if not isinstance(components, list):
        return None

    normalized = file_path.replace("\\", "/")
    best: tuple[int, str] | None = None
    for component in components:
        if not isinstance(component, dict):
            continue
        path = str(component.get("path", "")).replace("\\", "/")
        role = component.get("role")
        if not path or not role:
            continue
        if normalized == path:
            return str(role)
        if normalized.startswith(path.rstrip("/") + "/") and (best is None or len(path) > best[0]):
            best = (len(path), str(role))
    return best[1] if best else None


def overview_status(repo_path: str, head_sha: str | None) -> dict:
    """Whether an overview exists and how far the repo has moved since it
    was written — for reporting, never for gating. Kept separate from
    load_overview so the "is it there?" question doesn't require caring
    about the answer's contents."""
    overview = load_overview(repo_path)
    if overview is None:
        return {"present": False}
    stored_sha = overview.get("head_sha")
    return {
        "present": True,
        "generated_at": overview.get("generated_at"),
        "head_sha": stored_sha,
        "current_head_sha": head_sha,
        "head_moved": bool(stored_sha and head_sha and stored_sha != head_sha),
    }
