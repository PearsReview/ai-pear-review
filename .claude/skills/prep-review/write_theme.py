"""Records one change-set theme: the overall reason a group of hunks exists.

    python .claude/skills/prep-review/write_theme.py --repo . \
        --id server-split --title "Split server.py into handlers and web" \
        --why "..." --source author-session

A theme is written once and referenced by id from many briefings
(write_briefing.py --theme), so 90 hunks from one refactor share one
explanation instead of 90 drifting copies.

Stored in .context/changeset.json, not .briefing/: per-hunk briefings are
pruned as the diff moves, but a theme is expensive to recreate — especially
an author-session one, which records what the author knew and no later
investigation can recover. Re-running with an existing --id replaces that
theme; others are kept. Prints the resulting theme list.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_TITLE_CHARS = 80
MAX_WHY_CHARS = 400
SOURCES = ("author-session", "investigated")


def changeset_path(repo: str) -> Path:
    return Path(repo) / ".context" / "changeset.json"


def head_sha(repo: str) -> str | None:
    result = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None  # no commits yet — the theme is still worth recording
    return result.stdout.strip() or None


def upsert_theme(repo: str, theme: dict) -> dict:
    path = changeset_path(repo)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    themes = [t for t in data.get("themes", []) if isinstance(t, dict) and t.get("id") != theme["id"]]
    themes.append(theme)
    data.update(
        {
            "base_sha": head_sha(repo),
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "themes": themes,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--id", required=True, help="short slug, e.g. server-split")
    parser.add_argument("--title", required=True)
    parser.add_argument("--why", required=True)
    parser.add_argument("--source", choices=SOURCES, required=True)
    args = parser.parse_args()

    theme_id, title, why = args.id.strip(), args.title.strip(), args.why.strip()
    if not theme_id or not title or not why:
        raise SystemExit("id, title and why must all be non-empty")
    if len(title) > MAX_TITLE_CHARS:
        raise SystemExit(f"title is {len(title)} chars, over {MAX_TITLE_CHARS}")
    if len(why) > MAX_WHY_CHARS:
        raise SystemExit(f"why is {len(why)} chars, over {MAX_WHY_CHARS}")

    data = upsert_theme(args.repo, {"id": theme_id, "title": title, "why": why, "source": args.source})
    json.dump(data["themes"], sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
