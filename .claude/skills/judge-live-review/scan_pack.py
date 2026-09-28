"""Lists items from qa_agent/live/'s result pack(s) that need (or don't
yet have) a Claude-authored accuracy verdict.

Run from this project's own root (not target_repo):

    python .claude/skills/judge-live-review/scan_pack.py [--pack PATH] [--todo-only]

Without --pack, scans every qa_agent/live/results/*_live_review_pack.json
file — one per target repo the live suite has been run against. Each
pack's items get cross-referenced against a sibling
<repo>_claude_review.json (written by write_verdict.py, not this script)
to compute a `state`:

  missing — no Claude verdict recorded yet for this item
  stale   — a verdict was recorded, but the item's content_hash has since
            changed (the diff/narration/reply text judged isn't what's in
            the pack now — the live suite was re-run against a moved diff)
  fresh   — already judged, and nothing about the item has changed

Matched by content_hash, not index: qa_agent/live/test_live_review.py's
_write_pack_json regenerates the whole pack fresh on every run, and a
hunk added/removed upstream shifts every later index — content_hash
identifies an item by what was actually judged, so a shift alone doesn't
manufacture false "missing" entries for items that were already judged.

Deliberately standalone (no import from qa_agent, no pytest/playwright
dependency) — same reasoning as prep-review/scan_hunks.py's own copy of
the app's hashing rules, see that file's module docstring. The
content_hash formula below is a mirrored COPY of
qa_agent/live/test_live_review.py's _item_content_hash: keep both in sync
by hand if either changes, and prefer changing neither — a drift here
means every existing verdict looks "stale" for no real reason.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RESULTS_DIR = REPO_ROOT / "qa_agent" / "live" / "results"


def content_hash(item: dict) -> str:
    material = "\x00".join(
        [
            item.get("category", ""),
            item.get("file_path", ""),
            item.get("diff") or "",
            item.get("question") or "",
            item.get("answer_text", ""),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _review_path_for(pack_path: Path) -> Path:
    # "<repo>_live_review_pack.json" -> "<repo>_claude_review.json", same dir
    stem = pack_path.name.removesuffix("_live_review_pack.json")
    return pack_path.with_name(f"{stem}_claude_review.json")


def _existing_verdicts(review_path: Path) -> dict[str, dict]:
    if not review_path.exists():
        return {}
    try:
        data = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {item["content_hash"]: item for item in data.get("items", []) if "content_hash" in item}


def scan(pack_path: Path) -> list[dict]:
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    existing = _existing_verdicts(_review_path_for(pack_path))

    out: list[dict] = []
    for item in pack.get("items", []):
        # Trust the pack's own stored hash if present (it should always
        # match a recompute — this is a paranoia check, not the source of
        # truth), recomputing only as a fallback for a hand-edited or
        # older-shaped pack file.
        chash = item.get("content_hash") or content_hash(item)
        prior = existing.get(chash)
        if prior is None:
            state = "missing"
        elif prior.get("content_hash") == chash:
            state = "fresh"
        else:
            state = "stale"
        out.append(
            {
                **item,
                "content_hash": chash,
                "state": state,
                "pack_path": str(pack_path),
                "repo": pack.get("repo"),
                "target_repo": pack.get("target_repo"),
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack", default=None, help="one pack JSON file (default: every pack under qa_agent/live/results/)"
    )
    parser.add_argument("--todo-only", action="store_true", help="only items needing work (missing or stale)")
    args = parser.parse_args()

    if args.pack:
        pack_paths = [Path(args.pack)]
    else:
        pack_paths = sorted(RESULTS_DIR.glob("*_live_review_pack.json"))
        if not pack_paths:
            print(
                f"no pack files found under {RESULTS_DIR} — run `pytest qa_agent/live/` first",
                file=sys.stderr,
            )

    items: list[dict] = []
    for pack_path in pack_paths:
        items.extend(scan(pack_path))

    if args.todo_only:
        items = [i for i in items if i["state"] != "fresh"]

    json.dump(items, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
