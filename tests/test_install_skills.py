"""install_skills.py — copying the prep skills into a repo being reviewed.

Two things here are contracts with other tools rather than with this app, so
they can break without anything failing at runtime:

- The destination path. `.claude/skills/<name>/SKILL.md` is what Claude Code
  and Cline both discover automatically; a skill written anywhere else is
  simply never offered, with no error.
- `name:` in the frontmatter. Cline requires it to equal the directory name,
  so a renamed directory silently stops working there while still working in
  Claude Code.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import install_skills
from install_skills import REVIEWER_SKILLS, SOURCE_DIR, InstallError, check_target, install, plan

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "someones-project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def test_installs_every_reviewer_skill(target_repo: Path):
    install(target_repo)
    for skill in REVIEWER_SKILLS:
        assert (target_repo / ".claude" / "skills" / skill / "SKILL.md").is_file()


def test_copies_are_byte_identical_to_this_app_s_own(target_repo: Path):
    install(target_repo)
    for skill, entries in plan(target_repo).items():
        assert [state for _, _, state in entries] == ["same"] * len(entries), skill


def test_judge_live_review_is_not_installed(target_repo: Path):
    """It reads this project's own qa_agent/live/ results — meaningless elsewhere."""
    install(target_repo)
    assert not (target_repo / ".claude" / "skills" / "judge-live-review").exists()


def test_pycache_is_not_copied(target_repo: Path):
    cache = SOURCE_DIR / "call-map" / "__pycache__"
    if not cache.exists():
        pytest.skip("no __pycache__ in the source tree to exclude")
    install(target_repo)
    assert not (target_repo / ".claude" / "skills" / "call-map" / "__pycache__").exists()


def test_rerun_writes_nothing(target_repo: Path):
    install(target_repo)
    second = install(target_repo)
    assert all("already up to date" in line for line in second)


def test_a_locally_edited_skill_survives_a_rerun(target_repo: Path):
    install(target_repo)
    edited = target_repo / ".claude" / "skills" / "prep-review" / "SKILL.md"
    edited.write_text("my own version", encoding="utf-8")

    lines = install(target_repo)

    assert edited.read_text(encoding="utf-8") == "my own version"
    assert any("--force" in line for line in lines)


def test_force_overwrites_a_locally_edited_skill(target_repo: Path):
    install(target_repo)
    edited = target_repo / ".claude" / "skills" / "prep-review" / "SKILL.md"
    edited.write_text("my own version", encoding="utf-8")

    install(target_repo, force=True)

    assert edited.read_bytes() == (SOURCE_DIR / "prep-review" / "SKILL.md").read_bytes()


def test_dry_run_writes_nothing(target_repo: Path):
    lines = install(target_repo, dry_run=True)
    assert not (target_repo / ".claude").exists()
    assert any("to write" in line for line in lines)


def test_refuses_a_directory_that_is_not_a_git_repo(tmp_path: Path):
    with pytest.raises(InstallError, match="not a git repository"):
        check_target(tmp_path)


def test_refuses_this_app_s_own_repo():
    with pytest.raises(InstallError, match="this app's own repo"):
        check_target(REPO_ROOT)


def test_every_source_skill_names_its_own_directory():
    """Cline requires name: == directory name; Claude Code tolerates drift."""
    for skill_dir in sorted(p for p in SOURCE_DIR.iterdir() if p.is_dir()):
        frontmatter = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        match = re.search(r"^name:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
        assert match, f"{skill_dir.name}/SKILL.md has no name: field"
        assert match.group(1) == skill_dir.name


def test_reviewer_skills_all_exist_in_the_source_tree():
    """A typo here would drop a skill from every install, silently."""
    for skill in REVIEWER_SKILLS:
        assert (SOURCE_DIR / skill / "SKILL.md").is_file(), skill


def test_cli_reports_the_destination(target_repo: Path):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "install_skills.py"), "--repo", str(target_repo)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert ".claude" in result.stdout
    assert (target_repo / ".claude" / "skills" / "call-map" / "scan_calls.py").is_file()


def test_cli_refuses_a_non_repo_with_a_clear_message(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "install_skills.py"), "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not a git repository" in (result.stdout + result.stderr)


def test_source_dir_is_the_discoverable_path():
    assert install_skills.SOURCE_DIR == REPO_ROOT / ".claude" / "skills"


# --- --with-reminders ---


def _stop_commands(repo: Path) -> list[str]:
    settings = json.loads((repo / install_skills.HOOK_SETTINGS).read_text(encoding="utf-8"))
    return [hook["command"] for group in settings["hooks"]["Stop"] for hook in group["hooks"]]


def test_reminders_are_opt_in(target_repo: Path):
    install(target_repo)
    assert not (target_repo / ".claude" / "hooks").exists()
    assert not (target_repo / ".clinerules").exists()
    assert not (target_repo / install_skills.HOOK_SETTINGS).exists()


def test_with_reminders_installs_the_hook_the_rule_and_registers_it(target_repo: Path):
    install(target_repo, with_reminders=True)
    for relative in install_skills.REMINDER_FILES.values():
        assert (target_repo / relative).read_bytes() == (REPO_ROOT / relative).read_bytes()
    assert _stop_commands(target_repo) == [install_skills.HOOK_COMMAND]


def test_the_registered_hook_matches_this_repo_s_own(target_repo: Path):
    """The installed registration runs the same command this repo's own
    .claude/settings.json does, so the two can't drift apart."""
    own = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    own_commands = [hook["command"] for group in own["hooks"]["Stop"] for hook in group["hooks"]]
    assert install_skills.HOOK_COMMAND in own_commands


def test_existing_local_settings_are_kept_and_not_duplicated(target_repo: Path):
    path = target_repo / install_skills.HOOK_SETTINGS
    path.parent.mkdir(parents=True)
    existing = {
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
    }
    path.write_text(json.dumps(existing), encoding="utf-8")

    install(target_repo, with_reminders=True)
    install(target_repo, with_reminders=True)

    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["permissions"] == existing["permissions"]
    assert _stop_commands(target_repo) == ["echo hi", install_skills.HOOK_COMMAND]


def test_unreadable_local_settings_are_left_alone(target_repo: Path):
    path = target_repo / install_skills.HOOK_SETTINGS
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    lines = install(target_repo, with_reminders=True)
    assert path.read_text(encoding="utf-8") == "{not json"
    assert any("skipped" in line and "settings.local.json" in line for line in lines)


def test_a_single_file_clinerules_is_not_touched(target_repo: Path):
    (target_repo / ".clinerules").write_text("my rules", encoding="utf-8")
    lines = install(target_repo, with_reminders=True)
    assert (target_repo / ".clinerules").read_text(encoding="utf-8") == "my rules"
    assert any("Cline reminder rule: skipped" in line for line in lines)


def test_dry_run_with_reminders_writes_nothing(target_repo: Path):
    install(target_repo, dry_run=True, with_reminders=True)
    assert not (target_repo / ".claude").exists()
    assert not (target_repo / ".clinerules").exists()


def test_the_installed_hook_runs_in_the_target_repo(target_repo: Path):
    """End to end: the copied hook finds the copied scanner and reminds
    about an unbriefed change."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    (target_repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([*git, "add", "app.py"], cwd=target_repo, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=target_repo, check=True)
    install(target_repo, with_reminders=True)
    (target_repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(target_repo / ".claude" / "hooks" / "briefing_reminder.py")],
        input=json.dumps({"session_id": "s1"}),
        capture_output=True,
        text=True,
        cwd=target_repo,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(target_repo)},
    )
    assert result.returncode == 0, result.stderr
    assert "no current review briefing" in json.loads(result.stdout)["systemMessage"]


# --- .git/info/exclude: installed skills stay out of the review ---


def _exclude_text(repo: Path) -> str:
    path = repo / ".git" / "info" / "exclude"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_install_excludes_the_skills_from_git_status(target_repo: Path):
    install(target_repo)
    for skill in REVIEWER_SKILLS:
        assert f"/.claude/skills/{skill}/" in _exclude_text(target_repo)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=target_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert ".claude/skills" not in status.stdout


def test_exclude_patterns_are_added_once(target_repo: Path):
    install(target_repo)
    first = _exclude_text(target_repo)
    install(target_repo)
    assert _exclude_text(target_repo) == first
    assert first.count(install_skills._SKILL_EXCLUDE_HEADER) == 1


def test_exclude_keeps_what_was_already_there(target_repo: Path):
    exclude = target_repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("my-own-pattern", encoding="utf-8")  # no trailing newline
    install(target_repo)
    lines = _exclude_text(target_repo).splitlines()
    assert lines[0] == "my-own-pattern"
    assert f"/.claude/skills/{REVIEWER_SKILLS[0]}/" in lines


def test_dry_run_leaves_exclude_alone(target_repo: Path):
    before = _exclude_text(target_repo)
    lines = install(target_repo, dry_run=True)
    assert _exclude_text(target_repo) == before
    assert any("would add to" in line for line in lines)


# --- --repos-file / --check-repos ---


def test_load_repos_file_skips_blanks_and_comments(tmp_path: Path):
    repos = tmp_path / "repos.txt"
    repos.write_text("# my repos\n\n  ../a  \n../b\n   # indented comment\n", encoding="utf-8")
    assert install_skills._load_repos_file(repos) == ["../a", "../b"]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "install_skills.py"), *args],
        capture_output=True,
        text=True,
    )


def test_repos_file_installs_into_each_repo_and_reports_failures(tmp_path: Path):
    good = []
    for name in ("one", "two"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        good.append(repo)
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()
    repos = tmp_path / "repos.txt"
    repos.write_text("\n".join(str(p) for p in [*good, not_a_repo]) + "\n", encoding="utf-8")

    result = _run_cli("--repos-file", str(repos))

    assert result.returncode == 0, result.stderr
    for repo in good:
        assert (repo / ".claude" / "skills" / "call-map" / "SKILL.md").is_file()
    assert "[FAIL]" in result.stdout
    assert str(not_a_repo) in result.stdout


def test_repos_file_dry_run_writes_nothing(target_repo: Path, tmp_path: Path):
    repos = tmp_path / "repos.txt"
    repos.write_text(f"{target_repo}\n", encoding="utf-8")
    result = _run_cli("--repos-file", str(repos), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry run" in result.stdout
    assert not (target_repo / ".claude").exists()


def test_check_repos_reports_outdated_then_ok_and_writes_nothing(target_repo: Path, tmp_path: Path):
    repos = tmp_path / "repos.txt"
    repos.write_text(f"{target_repo}\n", encoding="utf-8")

    before = _run_cli("--check-repos", str(repos))
    assert before.returncode == 0, before.stderr
    assert "[WARN]" in before.stdout
    assert not (target_repo / ".claude").exists()

    install(target_repo)
    after = _run_cli("--check-repos", str(repos))
    assert "[OK  ]" in after.stdout


def test_a_missing_repos_file_is_a_clear_error(tmp_path: Path):
    result = _run_cli("--check-repos", str(tmp_path / "nope.txt"))
    assert result.returncode != 0
    assert "Could not read" in result.stderr
