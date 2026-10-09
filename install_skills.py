"""Copy the prep skills into the repo you want to review.

    python install_skills.py                    # into the current directory
    python install_skills.py --repo ../myapp
    python install_skills.py --repos-file repos.txt   # batch: one path per line
    python install_skills.py --check-repos repos.txt  # report freshness, write nothing
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

--with-auto-brief installs the same files, but registers the hook in its
blocking mode: when a Claude Code session stops with unbriefed hunks in
files it edited, the hook has it carry on and run prep-review there and
then (see .claude/hooks/briefing_reminder.py). It replaces the reminder
hook if that was registered, and the other way round.
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
# call-map is left out while the app doesn't read its output (see
# vscode/TODO.md).
REVIEWER_SKILLS = ("project-overview", "prep-review")

# --with-reminders: report name -> file, relative to both this app and the
# target repo. The hook needs prep-review's scan_hunks.py, which the skills
# install always puts beside it.
REMINDER_FILES = {
    "reminder hook": Path(".claude/hooks/briefing_reminder.py"),
    "Cline reminder rule": Path(".clinerules/prep-review-reminder.md"),
}
HOOK_COMMAND = 'python "$CLAUDE_PROJECT_DIR/.claude/hooks/briefing_reminder.py"'
AUTO_BRIEF_HOOK_COMMAND = f"{HOOK_COMMAND} --auto-brief"
HOOK_SETTINGS = Path(".claude/settings.local.json")

# Build artefacts of the scripts, created by running them. Copying them
# would put a stale .pyc next to a newer .py in someone else's repo.
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}

# .git/info/exclude: patterns added when skills are installed so they don't
# appear as untracked files in the reviewer's own `git status`. Mirrors what
# session_store.ensure_artifacts_ignored does for the app's runtime files.
_SKILL_EXCLUDE_HEADER = "# AI Pear Review prep skills (auto-excluded by install_skills.py):"
_SKILL_EXCLUDE_PATTERNS = tuple(f"/.claude/skills/{s}/" for s in REVIEWER_SKILLS)
# The reminder files and the personal settings the hook is registered in.
# Left out, they show up in the review as unbriefed changes that nobody in
# the session wrote (seen in a real auto-brief run).
_REMINDER_EXCLUDE_PATTERNS = (
    *(f"/{path.as_posix()}" for path in REMINDER_FILES.values()),
    f"/{HOOK_SETTINGS.as_posix()}",
)


class InstallError(Exception):
    """Something about the target repo means we should not write to it."""


def _exclude_skills_from_git(repo: Path, dry_run: bool = False, with_reminders: bool = False) -> str | None:
    """Adds the prep skill directories, and with_reminders the reminder
    files and settings.local.json, to .git/info/exclude.

    Returns a one-line status string if anything was added, None if already
    present. Best-effort: a missing .git directory, read-only files, or
    worktrees whose .git is a file are silently skipped.
    """
    info_dir = repo / ".git" / "info"
    if not (repo / ".git").is_dir():
        return None
    exclude = info_dir / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        patterns = _SKILL_EXCLUDE_PATTERNS + (_REMINDER_EXCLUDE_PATTERNS if with_reminders else ())
        missing = [p for p in patterns if p.strip("/") not in existing]
        if not missing:
            return None
        if not dry_run:
            info_dir.mkdir(parents=True, exist_ok=True)
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            exclude.write_text(
                f"{existing}{prefix}\n{_SKILL_EXCLUDE_HEADER}\n" + "".join(f"{p}\n" for p in missing),
                encoding="utf-8",
            )
        verb = "would add to" if dry_run else "added to"
        return f"  .git/info/exclude: {verb} .git/info/exclude ({len(missing)} pattern(s))"
    except OSError as exc:
        return f"  .git/info/exclude: could not update ({exc})"


def _load_repos_file(path: Path) -> list[str]:
    """Reads repo paths from a file, one per line. Ignores blank lines and # comments."""
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


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


def register_hook(repo: Path, dry_run: bool = False, auto_brief: bool = False) -> str:
    """Adds the hook to settings.local.json's Stop hooks, in reminder or
    auto-brief mode, keeping everything already there except this app's
    hook in the other mode (with both, the reminder would nag about what
    is being briefed). Returns one report line."""
    path = repo / HOOK_SETTINGS
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return f"  hook registration: skipped — {HOOK_SETTINGS.as_posix()} isn't readable JSON ({exc})"
    if not isinstance(settings, dict):
        return f"  hook registration: skipped — {HOOK_SETTINGS.as_posix()} isn't a JSON object"

    wanted, other = (AUTO_BRIEF_HOOK_COMMAND, HOOK_COMMAND) if auto_brief else (HOOK_COMMAND, AUTO_BRIEF_HOOK_COMMAND)
    mode = "auto-brief" if auto_brief else "reminder"
    stop = settings.setdefault("hooks", {}).setdefault("Stop", [])
    commands = [hook.get("command") for group in stop for hook in group.get("hooks", [])]
    replacing = other in commands
    if wanted in commands and not replacing:
        return f"  hook registration ({mode}): already in {HOOK_SETTINGS.as_posix()}"
    for group in stop:
        group["hooks"] = [hook for hook in group.get("hooks", []) if hook.get("command") != other]
    stop[:] = [group for group in stop if group["hooks"]]
    if wanted not in commands:
        stop.append({"hooks": [{"type": "command", "command": wanted, "timeout": 20}]})
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    note = f", replacing the {'reminder' if auto_brief else 'auto-brief'} hook" if replacing else ""
    return f"  hook registration ({mode}): {'to add to' if dry_run else 'added to'} {HOOK_SETTINGS.as_posix()}{note}"


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


def install(
    repo: Path,
    force: bool = False,
    dry_run: bool = False,
    with_reminders: bool = False,
    auto_brief: bool = False,
) -> list[str]:
    """Copies what's missing or outdated. Returns one report line per skill
    (and per reminder piece, with_reminders or auto_brief: the same files,
    with the hook registered in its blocking mode for auto_brief).

    Never silently overwrites a file whose content differs: that file may be
    your own edit of a skill, and losing it is worse than leaving the install
    incomplete. Those are reported and skipped unless force is set.
    """
    with_reminders = with_reminders or auto_brief
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
        lines.append(register_hook(repo, dry_run, auto_brief))
    if conflicts and not force:
        lines.append("")
        lines.append(f"{conflicts} file(s) differ from this app's copy and were kept. Pass --force to overwrite them.")
    exclude_line = _exclude_skills_from_git(repo, dry_run, with_reminders)
    if exclude_line:
        lines.append(exclude_line)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="the repo to install into (default: the current directory)")
    parser.add_argument(
        "--repos-file", metavar="FILE", help="file of repo paths (one per line) to install into in batch"
    )
    parser.add_argument(
        "--check-repos",
        metavar="FILE",
        help="report which repos listed in FILE have outdated or missing skills; writes nothing",
    )
    parser.add_argument("--force", action="store_true", help="overwrite files that differ from this app's copy")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument(
        "--with-reminders",
        action="store_true",
        help="also install the Claude Code Stop hook and Cline rule that remind you to run prep-review",
    )
    parser.add_argument(
        "--with-auto-brief",
        action="store_true",
        help="as --with-reminders, but the Stop hook has Claude Code run prep-review on the hunks it edited "
        "before it stops (adds roughly 14 s a hunk to the end of a turn)",
    )
    args = parser.parse_args()

    if args.check_repos:
        _cmd_check_repos(Path(args.check_repos))
        return

    if args.repos_file:
        _cmd_batch_install(
            Path(args.repos_file),
            force=args.force,
            dry_run=args.dry_run,
            with_reminders=args.with_reminders,
            auto_brief=args.with_auto_brief,
        )
        return

    try:
        repo = check_target(Path(args.repo))
    except InstallError as exc:
        raise SystemExit(f"Not installing — {exc}") from exc

    print(f"Prep skills -> {repo / '.claude' / 'skills'}\n")
    for line in install(
        repo,
        force=args.force,
        dry_run=args.dry_run,
        with_reminders=args.with_reminders,
        auto_brief=args.with_auto_brief,
    ):
        print(line)
    if args.dry_run:
        return
    print(
        "\nOpen your coding agent (Claude Code or Cline) in that repo and run the\n"
        "project-overview and prep-review skills before starting a review.\n"
        "The files they write (.context/, .briefing/) are read by this app.\n"
        "\nThe installed files were added to .git/info/exclude, so they stay out of\n"
        "the review. Commit them (or add them to .gitignore) if you'd rather share\n"
        "them with your team."
    )


def _cmd_check_repos(repos_file: Path) -> None:
    try:
        repo_paths = _load_repos_file(repos_file)
    except OSError as exc:
        raise SystemExit(f"Could not read {repos_file}: {exc}") from exc

    n = len(repo_paths)
    print(f"Skill freshness check ({n} repo{'s' if n != 1 else ''}):\n")
    for rp in repo_paths:
        try:
            repo = check_target(Path(rp).expanduser())
        except InstallError as exc:
            print(f"  [FAIL] {rp}  —  {exc}")
            continue
        outdated = sum(1 for entries in plan(repo).values() for _, _, state in entries if state != "same")
        label = str(repo)
        if outdated:
            hint = f"python install_skills.py --repo {repo}"
            print(f"  [WARN] {label}  —  {outdated} file(s) outdated, run: {hint}")
        else:
            print(f"  [OK  ] {label}  —  all skills up to date")


def _cmd_batch_install(
    repos_file: Path,
    force: bool = False,
    dry_run: bool = False,
    with_reminders: bool = False,
    auto_brief: bool = False,
) -> None:
    try:
        repo_paths = _load_repos_file(repos_file)
    except OSError as exc:
        raise SystemExit(f"Could not read {repos_file}: {exc}") from exc

    n = len(repo_paths)
    mode = "dry run" if dry_run else "install"
    print(f"Batch {mode} ({n} repo{'s' if n != 1 else ''}):\n")
    for rp in repo_paths:
        try:
            repo = check_target(Path(rp).expanduser())
        except InstallError as exc:
            print(f"{rp}\n  [FAIL] {exc}\n")
            continue
        print(f"{repo}")
        for line in install(repo, force=force, dry_run=dry_run, with_reminders=with_reminders, auto_brief=auto_brief):
            print(line)
        print()


if __name__ == "__main__":
    sys.exit(main())
