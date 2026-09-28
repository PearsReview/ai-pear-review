"""Per-hunk investigative briefing, so the conversation agent's
present_hunk() has more to go on than the raw diff alone.

Briefings arrive by one of two routes. The prep-review skill, run inside
the reviewer's own Claude Code session, investigates with Read, Grep and
git log/blame, and writes what it finds to the on-disk cache this module
reads. Otherwise a lighter briefing is generated on demand over the same
connection the conversation persona uses (see
ConversationClient.generate_once): one call, no tools, less thorough than
the skill's investigation but immediate. The app itself never drives the
Claude Code CLI.

Either way the briefing is cached to disk under file_path plus hunk
header, and validated against a hash of the hunk's exact diff. A hunk
whose code changed since it was cached is treated as stale and
regenerated, never shown against different code.

Context derived from grep or from a model is heuristic, not ground truth:
dynamic dispatch, decorators and name overloading all fool it. Good enough
to steer a conversational presentation, not to rely on.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..utils.debug_files import write_debug_prompt
from ..utils.json_utils import extract_json_object
from .conversation_service import ConversationClient, ConversationError
from .diff_service import Hunk

_DEBUG_DIR = ".briefing_debug"
_CACHE_DIR = ".briefing"

# Grammar-constrained decoding for the briefing (see
# ConversationClient.generate_once's response_schema).
#
# The schema matters because an unparseable response makes analyze_hunk
# raise ConversationError, and the hunk then gets no briefing at all —
# losing `intent`, the field that actually reaches the narration prompt,
# not just the optional ones. It fails quietly, looking like "no briefing
# was cached" rather than a failure.
#
# Measured on this codebase's model (qwen2.5-coder:7b): 3 of 18
# unconstrained calls came back unparseable, against 0 of 10 with this
# schema. Small samples, but the direction matches what generate_once's own
# docstring records — a free-text template was followed under half
# the time where the equivalent schema was ~4x more reliable.
#
# alternatives_considered is nullable rather than required: null is a
# normal answer (see BRIEFING_SYSTEM), and requiring it would push the
# model to invent content to satisfy the grammar.
#
# risk_notes is absent from this schema on purpose. The local model cannot
# make that judgment — measured across a 2x2 of prompt wording and schema,
# it caught 2 of 2 real risks only when completely unconstrained, and in
# that state also fired on 3 of 4 trivial hunks and fabricated a claim that
# max() "could have a higher time complexity". Any constraint, from either
# direction, collapsed it to null. Two modes, no judgment in between. The
# prep-review skill still writes it — a capable model with the repo and
# tools is a different proposition — and _hunk_prompt trusts it from that
# source only.
_BRIEFING_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "description": "Why this hunk looks the way it does, in one or two sentences.",
        },
        "alternatives_considered": {
            "type": ["string", "null"],
            "description": "Other approaches visibly weighed, or null.",
        },
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["intent", "confidence"],
    "additionalProperties": False,
}


# Kinds whose diff misleads on its own (a move reads as a deletion, a
# mechanical rename as N unrelated edits) — the prompt adds summary and
# related hunks for these even when the diff fits. See _briefing_lines.
MISLEADING_DIFF_KINDS = ("move", "mechanical")

# Match the prep-review skill's write_briefing.py caps. The skill refuses
# longer text; truncating here is the safety net for a hand-edited file.
_MAX_SUMMARY_CHARS = 300
_MAX_RELATED = 5
_MAX_NOTE_CHARS = 120


@dataclass
class Briefing:
    intent: str
    alternatives_considered: str | None
    risk_notes: str | None  # only ever set by the prep-review skill; see _BRIEFING_SCHEMA
    confidence: str  # "high" | "low"
    source: str = "generated"  # "generated" (local model) | "prep-review-skill" (Claude, with tools)
    # Skill-only fields (the local model never writes them). summary is what
    # the change does, for when the diff can't be shown; theme is an id into
    # .context/changeset.json (see changeset.py); related is a tuple of
    # {"file_path", "header", "relation", "note"} dicts.
    summary: str | None = None
    kind: str | None = None
    theme: str | None = None
    related: tuple[dict, ...] = ()

    @classmethod
    def unavailable(cls) -> Briefing:
        """Used when no briefing could be produced at all — no valid cache
        and no live connection to generate one — distinct from
        confidence="low", which means generation succeeded but genuinely
        found nothing beyond the diff."""
        return cls(intent="", alternatives_considered=None, risk_notes=None, confidence="low")

    @property
    def usable(self) -> bool:
        """Whether this briefing may be put in front of the model at all —
        a "low" one is recorded but never surfaced (see write_briefing.py)."""
        return self.confidence == "high" and bool(self.intent)

    @property
    def diff_misleads(self) -> bool:
        return self.kind in MISLEADING_DIFF_KINDS


@dataclass(frozen=True)
class RelatedHunk:
    """A briefing's related entry, resolved against the live diff: `index`
    is the session.hunks position to jump to, `summary` that hunk's own
    briefing summary when it has a usable one."""

    index: int
    file_path: str
    relation: str
    note: str | None
    summary: str | None


@dataclass(frozen=True)
class ChangeContext:
    """What surrounds one hunk in the change as a whole — its theme and the
    other hunks it belongs with. Built per hunk by app/web/context.py's
    change_context; None anywhere means "nothing known", not an error."""

    theme_title: str | None
    theme_why: str | None
    related: tuple[RelatedHunk, ...] = ()


def _hunk_content_hash(diff_context: str) -> str:
    """Identifies *this exact diff content* — not just this hunk's
    position — so a cached briefing is correctly treated as stale (and
    regenerated) if the underlying code changes since it was cached, even
    if the hunk's header/line-range coincidentally didn't shift."""
    return hashlib.sha256(diff_context.encode("utf-8")).hexdigest()


def _pregenerated_briefing_path(repo_path: str, hunk: Hunk) -> Path:
    """Cache key: file_path + a short hash of the hunk's header — stable
    across hunk-index drift between whenever this was written (the
    prep-review Skill, or a previous run of this app) and whenever it's
    read, unlike using session-local hunk.index alone."""
    safe_file = hunk.file_path.replace("/", "__").replace("\\", "__")
    header_hash = hashlib.sha256(hunk.header.encode("utf-8")).hexdigest()[:8]
    return Path(repo_path) / _CACHE_DIR / f"{safe_file}__{header_hash}.json"


def _capped(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit].strip() or None


def _related_entries(value: object) -> tuple[dict, ...]:
    """Well-formed related entries only, capped — skill-written, but a
    hand-edited file must not be able to crash a narration."""
    if not isinstance(value, list):
        return ()
    entries = []
    for entry in value[:_MAX_RELATED]:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("file_path"), str)
            and isinstance(entry.get("relation"), str)
        ):
            entries.append(
                {
                    "file_path": entry["file_path"],
                    "header": entry.get("header") if isinstance(entry.get("header"), str) else None,
                    "relation": entry["relation"],
                    "note": _capped(entry.get("note"), _MAX_NOTE_CHARS),
                }
            )
    return tuple(entries)


def prune_briefing_cache(repo_path: str, hunks: list[Hunk]) -> None:
    """Removes cached briefings that no longer describe any current hunk.

    The cache key is file_path + hash(header), not content, so a hunk whose
    header shifts by even one line writes a new file and orphans the old
    one. Keeping exactly the files a current hunk would load — same key,
    same content hash — clears the orphans and the stale entries and
    nothing else.

    Deleting the whole directory instead would throw away prep-review
    briefings for hunks that had not changed at all, on every Act Now
    confirm."""
    cache_dir = Path(repo_path) / _CACHE_DIR
    if not cache_dir.is_dir():
        return
    keep = {_pregenerated_briefing_path(repo_path, hunk).name: _hunk_content_hash(hunk.diff_context) for hunk in hunks}
    for path in cache_dir.glob("*.json"):
        try:
            if (
                path.name in keep
                and json.loads(path.read_text(encoding="utf-8")).get("content_hash") == keep[path.name]
            ):
                continue
            path.unlink()
        except (OSError, json.JSONDecodeError, AttributeError):
            try:
                path.unlink()  # unreadable is as useless as stale
            except OSError:
                pass  # caching is a nice-to-have, never worth failing the request over


class BriefingClient:
    """Loads a cached briefing if one's valid for this exact hunk content;
    otherwise generates one on demand via the conversation connection
    passed into analyze_hunk() (see module docstring for why this never
    touches the CLI itself)."""

    def __init__(self, config: dict, repo_path: str = ".") -> None:
        self.repo_path = repo_path
        # Structured JSON needs more room than a short conversational
        # turn — kept separate from conversation.max_tokens.
        self.max_tokens = config.get("max_tokens", 600)

    def analyze_hunk(self, hunk: Hunk, conversation: ConversationClient | None) -> Briefing:
        """Cache-first: a valid cached briefing for this exact diff content
        wins outright, so a live connection is only ever exercised on a
        cache miss. `conversation=None` (no live connection available)
        falls through to Briefing.unavailable() on a miss, the same
        "nothing to show" outcome a failed generation call would have
        produced — this method never raises for that reason alone."""
        cached = self.load_cached(hunk)
        if cached is not None:
            return cached

        if conversation is None:
            # Degraded mode (no live connection) and nothing cached —
            # same "nothing to show" outcome a failed generation call
            # would have produced.
            return Briefing.unavailable()

        briefing_system = conversation.prompts.briefing_system  # tier-selected, see app/prompts/
        prompt = f"File: {hunk.file_path}\n\nDiff hunk:\n```\n{hunk.diff_context}\n```"
        write_debug_prompt(_DEBUG_DIR, f"hunk_{hunk.index}.txt", f"{briefing_system}\n\n{prompt}")

        text = conversation.generate_once(
            prompt,
            system_prompt=briefing_system,
            max_tokens=self.max_tokens,
            response_schema=_BRIEFING_SCHEMA,
        )
        try:
            # Not json.loads: Anthropic has no schema-constrained output, so
            # its reply can arrive wrapped in a ```json fence.
            data = extract_json_object(text)
        except json.JSONDecodeError as exc:
            raise ConversationError(f"briefing agent did not return valid JSON: {exc}") from exc

        briefing = Briefing(
            intent=data.get("intent") or "",
            alternatives_considered=data.get("alternatives_considered"),
            risk_notes=None,  # never from the local model — see _BRIEFING_SCHEMA
            confidence=data.get("confidence") or "low",
            source="generated",
        )
        self._write_cache(hunk, briefing, source="generated")
        return briefing

    def load_cached(self, hunk: Hunk) -> Briefing | None:
        """The cache read alone, never generating. Public for a hunk too
        large to send to the model: generating a briefing would ship the
        same oversized diff (see handlers/narration.py's oversized path)."""
        path = _pregenerated_briefing_path(self.repo_path, hunk)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if data.get("content_hash") != _hunk_content_hash(hunk.diff_context):
            return None  # stale — the underlying diff changed since this was written
        intent = data.get("intent")
        if not intent:
            return None  # a Skill-written skeleton not filled in yet — not usable
        return Briefing(
            intent=intent,
            alternatives_considered=data.get("alternatives_considered"),
            risk_notes=data.get("risk_notes"),
            confidence=data.get("confidence") or "low",
            # Carried through because _hunk_prompt trusts risk_notes only
            # from the skill. Defaults to "generated" for cache files
            # reading: an unlabelled note is treated as the local model's,
            # which is the conservative assumption.
            source=data.get("source") or "generated",
            summary=_capped(data.get("summary"), _MAX_SUMMARY_CHARS),
            kind=data.get("kind") if isinstance(data.get("kind"), str) else None,
            theme=data.get("theme") if isinstance(data.get("theme"), str) else None,
            related=_related_entries(data.get("related")),
        )

    def _write_cache(self, hunk: Hunk, briefing: Briefing, source: str) -> None:
        path = _pregenerated_briefing_path(self.repo_path, hunk)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "file_path": hunk.file_path,
                        "header": hunk.header,
                        "content_hash": _hunk_content_hash(hunk.diff_context),
                        "intent": briefing.intent,
                        "alternatives_considered": briefing.alternatives_considered,
                        "risk_notes": briefing.risk_notes,
                        "confidence": briefing.confidence,
                        "source": source,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # caching is a nice-to-have, never worth failing the request over
