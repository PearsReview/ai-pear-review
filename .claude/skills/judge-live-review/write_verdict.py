"""Records one Claude-authored accuracy verdict for one item from a
qa_agent/live/ pack, and regenerates the human-readable summary alongside
it.

    python .claude/skills/judge-live-review/write_verdict.py \\
        --pack qa_agent/live/results/<target>_live_review_pack.json \\
        --content-hash <hash from scan_pack.py> \\
        --verdict yes|no|unsure \\
        --reason "One sentence: what's actually right or wrong about it."

Exists so a verdict can't end up in a shape scan_pack.py won't recognize
on the next run (a content_hash that doesn't match anything real would
silently never show as "fresh"), same reasoning as prep-review's
write_briefing.py. --content-hash is copied verbatim from scan_pack.py's
output, never retyped or recomputed by hand.

Writes/updates two files next to the pack, both named from its own
"<repo>_live_review_pack.json" stem:
  <repo>_claude_review.json — one record per judged item, keyed by
    content_hash. The machine-readable form scan_pack.py reads back to
    compute missing/stale/fresh on the next run.
  <repo>_claude_review.md — regenerated from the JSON on every write,
    disagreements with the pack's own local-judge verdict listed FIRST
    (see _sort_key below) — that's the signal actually worth a human's
    attention, everywhere else being simple agreement.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_VALID_VERDICTS = ("yes", "no", "unsure")


def _review_path_for(pack_path: Path) -> Path:
    stem = pack_path.name.removesuffix("_live_review_pack.json")
    return pack_path.with_name(f"{stem}_claude_review.json")


def _find_item(pack: dict, content_hash: str) -> dict:
    for item in pack.get("items", []):
        if item.get("content_hash") == content_hash:
            return item
    raise SystemExit(
        f"content_hash {content_hash!r} not found in {pack.get('repo', '?')}'s pack — "
        "copy it verbatim from scan_pack.py's output, don't retype it"
    )


def _load_review(path: Path, repo: str) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"repo": repo, "generated_at": datetime.now(timezone.utc).isoformat(), "items": []}


def _sort_key(record: dict) -> tuple:
    # False sorts before True: a real disagreement (agrees_with_local is
    # literally False) gets key False and sorts first; True and None (no
    # local verdict to compare against — not a disagreement) both get key
    # True and sort after, in their original relative order (sorted() is
    # stable).
    return (record.get("agrees_with_local") is not False,)


def _write_markdown(review_path: Path, review: dict) -> None:
    md_path = review_path.with_suffix(".md")
    items = sorted(review["items"], key=_sort_key)
    disagreement_count = sum(1 for i in items if i.get("agrees_with_local") is False)

    lines = [f"# Claude review — {review['repo']}", ""]
    lines.append(f"{len(items)} item(s) judged. {disagreement_count} disagreement(s) with the local judge.")
    lines.append("")

    seen_disagreement_header = False
    seen_rest_header = False
    for record in items:
        is_disagreement = record.get("agrees_with_local") is False
        if is_disagreement and not seen_disagreement_header:
            lines.append("## Disagreements with the local judge — read these first")
            lines.append("")
            seen_disagreement_header = True
        elif not is_disagreement and disagreement_count and not seen_rest_header:
            lines.append("## Everything else")
            lines.append("")
            seen_rest_header = True

        lines.append(f"### {record['file_path']} — {record['category']}")
        lines.append("")
        if record.get("question"):
            lines.append(f"**Question:** {record['question']}")
        if record.get("diff"):
            lines.append("```diff")
            lines.append(record["diff"].rstrip("\n"))
            lines.append("```")
        lines.append("")
        lines.append(f"**Answer:** {record.get('answer_text', '')}")
        lines.append("")
        local = record.get("local_verdict") or {}
        lines.append(f"**Local judge (Ollama):** {local.get('verdict', 'n/a')} — {local.get('reason', '')}")
        lines.append(f"**Claude verdict:** {record['verdict']} — {record['reason']}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", required=True, help="path to a *_live_review_pack.json")
    parser.add_argument("--content-hash", required=True, help="from scan_pack.py — never computed by hand")
    parser.add_argument("--verdict", required=True, choices=_VALID_VERDICTS)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    if len(args.content_hash) != 64:
        raise SystemExit(f"content_hash should be a 64-char sha256 hex from scan_pack.py, got {len(args.content_hash)}")
    if not args.reason.strip():
        raise SystemExit("reason is empty — a verdict with no reason isn't reviewable by anyone reading the summary")

    pack_path = Path(args.pack)
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    item = _find_item(pack, args.content_hash)

    review_path = _review_path_for(pack_path)
    review = _load_review(review_path, pack.get("repo", pack_path.stem))

    local_verdict = item.get("local_verdict") or {}
    agrees_with_local = (local_verdict.get("verdict") == args.verdict) if local_verdict.get("verdict") else None

    record = {
        "content_hash": args.content_hash,
        "index": item.get("index"),
        "category": item.get("category"),
        "file_path": item.get("file_path"),
        "diff": item.get("diff"),
        "question": item.get("question"),
        "answer_text": item.get("answer_text"),
        "local_verdict": local_verdict,
        "verdict": args.verdict,
        "reason": args.reason.strip(),
        "agrees_with_local": agrees_with_local,
        "judged_at": datetime.now(timezone.utc).isoformat(),
    }

    review["items"] = [i for i in review["items"] if i.get("content_hash") != args.content_hash]
    review["items"].append(record)
    review["updated_at"] = datetime.now(timezone.utc).isoformat()

    review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")
    _write_markdown(review_path, review)

    print(f"wrote {review_path}", file=sys.stderr)
    if agrees_with_local is False:
        print(
            f"  disagrees with local judge ({local_verdict.get('verdict')} -> {args.verdict}) — "
            "flagged first in the .md summary",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
