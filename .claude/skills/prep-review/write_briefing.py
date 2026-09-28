"""Writes one briefing into the app's .briefing/ cache, with the hash and
shape the app requires.

    python .claude/skills/prep-review/write_briefing.py \
        --repo . --content-hash <hash from scan_hunks.py> \
        --file-path app/server.py --header '@@ -10,6 +10,9 @@' \
        --intent "..." [--alternatives "..."] [--risks "..."] \
        [--summary "..."] [--kind move] [--theme server-split] \
        [--related '[{"file_path": "...", "header": "@@ ... @@", "relation": "moved_to", "note": "..."}]'] \
        [--confidence high|low]

Exists so a briefing can't be written in a shape the app quietly drops.
Three ways that happens, none of which produce any error at review time:
  - content_hash not matching the hunk's diff exactly -> treated as stale
  - empty intent -> treated as an unfilled skeleton
  - confidence anything but "high" -> written, cached, and then never
    surfaced, because the app only folds a briefing into a prompt when
    confidence == "high"
The last one surprises people: a "low" briefing is not a weaker hint, it
is an unused one. Pass --confidence low deliberately (to record that a
hunk was investigated and nothing was found), not as a hedge.

The length caps below match app/services/briefing_service.py's. The app
truncates rather than rejects, so exceeding them here is refused up front
instead of silently losing the end of a sentence at review time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

KINDS = ("behaviour", "move", "mechanical", "docs", "test")
RELATIONS = ("moved_to", "moved_from", "caller_of", "called_by", "test_for", "tested_by", "docs_for", "same_edit")
MAX_SUMMARY_CHARS = 300
MAX_RELATED = 5
MAX_NOTE_CHARS = 120


def briefing_path(repo: str, file_path: str, header: str) -> Path:
    safe_file = file_path.replace("/", "__").replace("\\", "__")
    header_hash = hashlib.sha256(header.encode("utf-8")).hexdigest()[:8]
    return Path(repo) / ".briefing" / f"{safe_file}__{header_hash}.json"


def parse_related(raw: str | None) -> list[dict]:
    """Validated related-hunk entries. Raises SystemExit with the reason."""
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--related is not valid JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise SystemExit("--related must be a JSON list")
    if len(entries) > MAX_RELATED:
        raise SystemExit(f"--related has {len(entries)} entries; keep the {MAX_RELATED} most useful")
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("file_path"), str):
            raise SystemExit(f"each related entry needs a file_path string, got {entry!r}")
        if entry.get("relation") not in RELATIONS:
            raise SystemExit(f"relation must be one of {RELATIONS}, got {entry.get('relation')!r}")
        note = entry.get("note") or None
        if note is not None and len(note) > MAX_NOTE_CHARS:
            raise SystemExit(f"related note is {len(note)} chars, over {MAX_NOTE_CHARS}: {note!r}")
        cleaned.append(
            {
                "file_path": entry["file_path"],
                "header": entry.get("header"),
                "relation": entry["relation"],
                "note": note,
            }
        )
    return cleaned


def known_theme_ids(repo: str) -> set[str] | None:
    path = Path(repo) / ".context" / "changeset.json"
    try:
        return {t.get("id") for t in json.loads(path.read_text(encoding="utf-8")).get("themes", [])}
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--file-path", required=True)
    parser.add_argument("--header", required=True)
    parser.add_argument("--content-hash", required=True, help="from scan_hunks.py — never computed by hand")
    parser.add_argument("--intent", required=True)
    parser.add_argument("--alternatives", default=None)
    parser.add_argument("--risks", default=None)
    parser.add_argument("--summary", default=None, help=f"what the code change does, <= {MAX_SUMMARY_CHARS} chars")
    parser.add_argument("--kind", choices=KINDS, default=None)
    parser.add_argument("--theme", default=None, help="a theme id written with write_theme.py")
    parser.add_argument("--related", default=None, help="JSON list of related hunks")
    parser.add_argument("--confidence", choices=("high", "low"), default="high")
    args = parser.parse_args()

    if not args.intent.strip():
        raise SystemExit("intent is empty — the app treats that as an unfilled skeleton and ignores the file")
    if len(args.content_hash) != 64:
        raise SystemExit(
            f"content_hash should be a 64-char sha256 hex from scan_hunks.py, got {len(args.content_hash)}"
        )
    summary = (args.summary or "").strip() or None
    if summary is not None and len(summary) > MAX_SUMMARY_CHARS:
        raise SystemExit(f"summary is {len(summary)} chars, over {MAX_SUMMARY_CHARS}")
    related = parse_related(args.related)
    if args.theme:
        themes = known_theme_ids(args.repo)
        if themes is None or args.theme not in themes:
            print(
                f"warning: theme {args.theme!r} is not in .context/changeset.json yet — "
                "write it with write_theme.py or the app will ignore it",
                file=sys.stderr,
            )

    path = briefing_path(args.repo, args.file_path, args.header)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "file_path": args.file_path,
                "header": args.header,
                "content_hash": args.content_hash,
                "intent": args.intent.strip(),
                "alternatives_considered": args.alternatives,
                "risk_notes": args.risks,
                "summary": summary,
                "kind": args.kind,
                "theme": args.theme,
                "related": related,
                "confidence": args.confidence,
                # Distinguishes these from the app's own on-demand briefings
                # ("generated"), which are written by the same cache writer.
                "source": "prep-review-skill",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
