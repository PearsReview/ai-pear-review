"""Copy the prep skills into the repo you want to review.

    python install_skills.py                    # into the current directory
    python install_skills.py --repo ../myapp
    python install_skills.py --dry-run          # say what would change, write nothing

The skills are instructions for *your* coding agent, not for this app: it
only ever reads the files they leave behind (see README.md's Prep Skills
section). They have to sit in the repo being reviewed, because that is the
repo your agent session is open on and the one the scripts scan by default.

`.claude/skills/` is the destination for both supported agents: Claude Code
reads it, and so does Cline (which also accepts `.cline/skills/` — one
folder serving both is why this installs to the Claude path for everyone).

Rerun it after updating this app; skills that already match are left alone,
and a copy you have edited yourself is reported and kept unless you pass
--force.

--with-reminders also installs the two "brief your changes before you stop"
reminders: a Claude Code Stop hook, registered in .claude/settings.local.json
(personal, not the team's shared settings.json), and a Cline rule under
.clinerules/. Opt-in, because it changes how the agent behaves in that repo
rather than only adding skills it can be asked to run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SOURCE_DIR = APP_DIR / ".claude" / "skills"

# judge-live-review is deliberately not here: it reads this project's own
# qa_agent/live/ results, so it means nothing in a repo being reviewed.
REVIEWER_SKILLS = ("project-overview", "call-map", "prep-review")

# --with-reminders: report name -> file, relative to both this app and the
# target repo. The hook needs prep-review's scan_hunks.py, which the skills
# install always puts beside it.
REMINDER_FILES = {
    "reminder hook": Path(".claude/hooks/briefing_reminder.py"),
    "Cline reminder rule": Path(".clinerules/prep-review-reminder.md"),
}
HOOK_COMMAND = 'python "$CLAUDE_PROJECT_DIR/.claude/hooks/briefing_reminder.py"'
HOOK_SETTINGS = Path(".claude/settings.local.json")

# Build artefacts of the scripts, created by running them. Copying them
# would put a stale .pyc next to a newer .py in someone else's repo.
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


class InstallError(Exception):
    """Something about the target repo means we should not write to it."""


def _source_files(skill: str) -> list[Path]:
    root = SOURCE_DIR / skill
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix not in _SKIP_SUFFIXES
        and not _SKIP_DIRS.intersection(path.relative_to(root).parts)
    )


def _state(source: Path, target: Path) -> str:
    if not target.exists():
        return "new"
    return "same" if target.read_bytes() == source.read_bytes() else "differs"


def plan(repo: Path, with_reminders: bool = False) -> dict[str, list[tuple[Path, Path, str]]]:
    """Per skill (and per reminder file, with_reminders), every (source,
    destination, state) this install would touch.

    States are "new" (nothing there), "same" (byte-identical, nothing to do)
    and "differs" (present but changed — either an edit of yours or an older
    version of this app's copy). Comparing bytes rather than mtimes keeps a
    rerun quiet after a checkout, which touches mtimes without changing
    content.
    """
    result: dict[str, list[tuple[Path, Path, str]]] = {}
    for skill in REVIEWER_SKILLS:
        entries = []
        for source in _source_files(skill):
            target = repo / ".claude" / "skills" / skill / source.relative_to(SOURCE_DIR / skill)
            entries.append((source, target, _state(source, target)))
        result[skill] = entries
    if with_reminders:
        for name, relative in REMINDER_FILES.items():
            source, target = APP_DIR / relative, repo / relative
            result[name] = [(source, target, _state(source, target))]
    return result


def _cline_rules_is_a_file(repo: Path) -> bool:
    """Cline also accepts .clinerules as one file; a rule can't be added
    beside it without editing that file, which is the user's."""
    return (repo / ".clinerules").is_file()


def register_hook(repo: Path, dry_run: bool = False) -> str:
    """Adds the reminder to settings.local.json's Stop hooks, keeping
    everything already there. Returns one report line."""
    path = repo / HOOK_SETTINGS
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return f"  hook registration: skipped — {HOOK_SETTINGS.as_posix()} isn't readable JSON ({exc})"
    if not isinstance(settings, dict):
        return f"  hook registration: skipped — {HOOK_SETTINGS.as_posix()} isn't a JSON object"

    stop = settings.setdefault("hooks", {}).setdefault("Stop", [])
    commands = [hook.get("command") for group in stop for hook in group.get("hooks", [])]
    if HOOK_COMMAND in commands:
        return f"  hook registration: already in {HOOK_SETTINGS.as_posix()}"
    stop.append({"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 20}]})
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return f"  hook registration: {'to add to' if dry_run else 'added to'} {HOOK_SETTINGS.as_posix()}"


def check_target(repo: Path) -> Path:
    """The resolved repo path, or InstallError explaining why it can't be used."""
    resolved = repo.resolve()
    if not resolved.is_dir():
        raise InstallError(f"{resolved} is not a directory")
    if not (resolved / ".git").exists():
        raise InstallError(
            f"{resolved} is not a git repository (no .git). The prep skills describe "
            "uncommitted changes, so there is nothing for them to read here."
        )
    if resolved == SOURCE_DIR.parent.parent:
        raise InstallError("That's this app's own repo — the skills already live here, in .claude/skills/.")
    return resolved


def install(repo: Path, force: bool = False, dry_run: bool = False, with_reminders: bool = False) -> list[str]:
    """Copies what's missing or outdated. Returns one report line per skill
    (and per reminder piece, with_reminders).

    Never silently overwrites a file whose content differs: that file may be
    your own edit of a skill, and losing it is worse than leaving the install
    incomplete. Those are reported and skipped unless force is set.
    """
    lines = []
    conflicts = 0
    for skill, entries in plan(repo, with_reminders).items():
        if skill == "Cline reminder rule" and _cline_rules_is_a_file(repo):
            lines.append(f"  {skill}: skipped — .clinerules is a single file here, not a folder")
            continue
        written = 0
        kept = 0
        for source, target, state in entries:
            if state == "same":
                continue
            if state == "differs" and not force:
                kept += 1
                continue
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            written += 1
        conflicts += kept
        if written and kept:
            lines.append(
                f"  {skill}: {written} file(s) {'to write' if dry_run else 'written'}, {kept} left alone (changed locally)"
            )
        elif written:
            lines.append(f"  {skill}: {written} file(s) {'to write' if dry_run else 'written'}")
        elif kept:
            lines.append(f"  {skill}: unchanged here, {kept} file(s) differ from this app's copy")
        else:
            lines.append(f"  {skill}: already up to date")
    if with_reminders:
        lines.append(register_hook(repo, dry_run))
    if conflicts and not force:
        lines.append("")
        lines.append(f"{conflicts} file(s) differ from this app's copy and were kept. Pass --force to overwrite them.")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="the repo to install into (default: the current directory)")
    parser.add_argument("--force", action="store_true", help="overwrite files that differ from this app's copy")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument(
        "--with-reminders",
        action="store_true",
        help="also install the Claude Code Stop hook and Cline rule that remind you to run prep-review",
    )
    args = parser.parse_args()

    try:
        repo = check_target(Path(args.repo))
    except InstallError as exc:
        raise SystemExit(f"Not installing — {exc}") from exc

    print(f"Prep skills -> {repo / '.claude' / 'skills'}\n")
    for line in install(repo, force=args.force, dry_run=args.dry_run, with_reminders=args.with_reminders):
        print(line)
    if args.dry_run:
        return
    print(
        "\nOpen your coding agent (Claude Code or Cline) in that repo and run the\n"
        "project-overview, call-map and prep-review skills before starting a review.\n"
        "The files they write (.context/, .briefing/) are read by this app.\n"
        "\nThe installed files are new, untracked files in that repo, so the review\n"
        "will list them as changes until you commit them or add them to .gitignore."
    )


if __name__ == "__main__":
    sys.exit(main())
