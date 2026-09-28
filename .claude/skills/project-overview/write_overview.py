"""Writes .context/project_overview.{json,md} for the review app to use.

    # check what's there already
    python .claude/skills/project-overview/write_overview.py --status

    # write one (built from a JSON file you prepared)
    python .claude/skills/project-overview/write_overview.py --from overview.json

Two outputs from one input, because they have different readers:
  - project_overview.json — what the APP reads. It injects only the
    digest plus the one component entry matching the file being reviewed,
    so this has to be structured for lookup, not for reading.
  - project_overview.md — what a HUMAN reads. Same content, laid out to
    be read top to bottom.

Length limits below are enforced, not advisory. The app runs a small
local model on a few thousand tokens total, and anything injected comes
out of the budget the diff itself needs — an overview that crowds out the
code makes narration worse, which is the opposite of the point.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_DIGEST_CHARS = 900  # the app truncates at 1000; leave headroom rather than land exactly on it
MAX_ROLE_CHARS = 350  # app truncates component roles at 400
MAX_COMPONENTS = 40

# The review app's own derived output. Normally gitignored, but not every
# repo does that — and where it isn't, committing an overview makes the
# overview itself show up as a newly added file, so --status would report
# "files were added, go update the overview" because you updated the
# overview. Excluded so the check can't trigger on its own footprints.
DERIVED_PREFIXES = (".context/", ".briefing/", ".review/")


def head_sha(repo: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def overview_path(repo: str) -> Path:
    return Path(repo) / ".context" / "project_overview.json"


def markdown_path(repo: str) -> Path:
    return Path(repo) / ".context" / "project_overview.md"


def validate(data: dict) -> list[str]:
    problems = []
    digest = (data.get("digest") or "").strip()
    if not digest:
        problems.append("digest is required — without it the app ignores the whole file")
    if len(digest) > MAX_DIGEST_CHARS:
        problems.append(f"digest is {len(digest)} chars, max {MAX_DIGEST_CHARS} (the app truncates past 1000)")

    components = data.get("components") or []
    if not isinstance(components, list):
        problems.append("components must be a list")
        return problems
    if len(components) > MAX_COMPONENTS:
        problems.append(f"{len(components)} components, max {MAX_COMPONENTS} — list areas, not every file")
    for component in components:
        if not isinstance(component, dict) or not component.get("path") or not component.get("role"):
            problems.append(f"each component needs a path and a role: {component!r}")
            continue
        if len(component["role"]) > MAX_ROLE_CHARS:
            problems.append(f"role for {component['path']} is {len(component['role'])} chars, max {MAX_ROLE_CHARS}")
    return problems


def render_markdown(data: dict) -> str:
    lines = [
        "# Project overview",
        "",
        f"_Generated {data.get('generated_at')} at `{(data.get('head_sha') or 'unknown')[:12]}`._",
        "_Written by the project-overview skill for the AI Pear Review app;",
        "regenerate rather than hand-editing, since the app reads the .json beside this._",
        "",
        "## What this project is",
        "",
        data.get("digest", ""),
        "",
    ]
    if data.get("components"):
        lines += ["## Main pieces", ""]
        for component in data["components"]:
            lines.append(f"- **`{component['path']}`** — {component['role']}")
        lines.append("")
    if data.get("entry_points"):
        lines += ["## Entry points", ""] + [f"- {e}" for e in data["entry_points"]] + [""]
    if data.get("conventions"):
        lines += ["## Conventions worth knowing", ""] + [f"- {c}" for c in data["conventions"]] + [""]
    return "\n".join(lines)


def structural_changes(repo: str, stored_sha: str) -> tuple[list[str], list[str]]:
    """(commit subjects, structural file changes) since the stored sha.

    "Structural" means added, deleted or renamed files — filtered on
    purpose. An overview describes a project's shape, and shape moves when
    files appear, vanish or move, not when someone edits the body of an
    existing one. A plain `git diff --stat` over a few hundred commits is
    mostly modified files, which is exactly the noise that makes a reviewer
    skip reading it and regenerate blindly.
    """
    log = subprocess.run(
        ["git", "-C", repo, "log", "--oneline", "--no-decorate", "-30", f"{stored_sha}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    names = subprocess.run(
        ["git", "-C", repo, "diff", "--name-status", "--diff-filter=ADR", f"{stored_sha}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return (
        [line for line in log.splitlines() if line],
        [line for line in names.splitlines() if line and not _is_derived(line)],
    )


def _is_derived(name_status_line: str) -> bool:
    """True for the review app's own output files. The line is
    "A\\tpath" (or "R100\\told\\tnew"), so every field after the status is
    checked — a rename has two paths and either could be the derived one."""
    paths = name_status_line.split("\t")[1:]
    return any(path.replace("\\", "/").startswith(DERIVED_PREFIXES) for path in paths)


def print_status(repo: str, path: Path, current: str | None) -> None:
    if not path.exists():
        print("no overview written yet (.context/project_overview.json missing)")
        print("nothing to update — study the repo and write one with --from")
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"overview exists but is unreadable ({exc}) - rewrite it")
        return

    stored = existing.get("head_sha")
    print(f"overview written {existing.get('generated_at')} at {str(stored)[:12]}")
    print(f"components: {len(existing.get('components') or [])}")

    if not (stored and current and stored != current):
        print("HEAD unchanged since it was written - nothing to do")
        return

    behind = subprocess.run(
        ["git", "-C", repo, "rev-list", "--count", f"{stored}..HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    commits, structural = structural_changes(repo, stored)
    print(f"HEAD has moved since ({behind or '?'} commits)")
    print()

    if structural:
        print("Files added/deleted/renamed since — the changes that can move a project's shape:")
        for line in structural[:40]:
            print(f"  {line}")
        if len(structural) > 40:
            print(f"  ... and {len(structural) - 40} more")
        print()
        print("Read these, then EDIT the existing overview where it's now wrong.")
        print("Only start from scratch if the project is genuinely unrecognisable.")
    else:
        print("No files added, deleted or renamed since — the project's shape almost")
        print("certainly hasn't moved, only the code inside it. Skim the log below;")
        print("if nothing there changed what the project *is*, just run --restamp.")

    if commits:
        print()
        print("Commits since:")
        for line in commits:
            print(f"  {line}")


def restamp(repo: str, path: Path, current: str | None) -> None:
    """Mark the existing overview as still accurate at the current HEAD.

    The cheap path, and the one that matters most. The honest common answer
    to "is this overview stale?" is "it's still accurate, just old" — and
    without this, that answer costs exactly as much as starting over, so
    nobody pays it and the overview rots. Content is untouched; only the
    stamp moves.
    """
    if not path.exists():
        raise SystemExit("no overview to restamp — write one with --from first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"overview is unreadable ({exc}) - rewrite it with --from") from exc

    problems = validate(data)
    if problems:
        # Refusing here rather than restamping: a file that fails validation
        # is one the app would ignore, and restamping it would report it as
        # fresh in the settings panel while it silently does nothing.
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit("existing overview no longer validates — rewrite it with --from")

    previous = data.get("head_sha")
    data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["head_sha"] = current
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    markdown_path(repo).write_text(render_markdown(data), encoding="utf-8")
    print(f"restamped {str(previous)[:12]} -> {str(current)[:12]}, content unchanged", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--from", dest="source", help="JSON file with digest/components/entry_points/conventions")
    parser.add_argument("--status", action="store_true", help="report what's already written, then exit")
    parser.add_argument(
        "--restamp",
        action="store_true",
        help="the existing overview is still accurate: move its stamp to the current HEAD, change nothing else",
    )
    args = parser.parse_args()

    path = overview_path(args.repo)
    current = head_sha(args.repo)

    if args.status:
        print_status(args.repo, path, current)
        return

    if args.restamp:
        restamp(args.repo, path, current)
        return

    if not args.source:
        raise SystemExit("--from <file.json> is required (or use --status / --restamp)")

    data = json.loads(Path(args.source).read_text(encoding="utf-8"))
    problems = validate(data)
    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit("overview not written — fix the above")

    data = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head_sha": current,
        "digest": data["digest"].strip(),
        "components": data.get("components") or [],
        "entry_points": data.get("entry_points") or [],
        "conventions": data.get("conventions") or [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    markdown_path(args.repo).write_text(render_markdown(data), encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)
    print(f"wrote {markdown_path(args.repo)}", file=sys.stderr)


if __name__ == "__main__":
    main()
