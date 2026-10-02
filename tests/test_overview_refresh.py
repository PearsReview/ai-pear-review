"""The project-overview skill's refresh path (--status / --restamp).

An overview is the most expensive prep file to produce — it needs a
capable model to read the whole repo — and the only one with no
incremental path. That combination rots it: the honest common answer to
"is this still right?" is "yes, just old", and if that answer costs the
same as starting over, nobody pays it.

--restamp is what makes the cheap answer cheap, and --status is what tells
you which answer applies. These tests pin both, including the case where
restamp must refuse.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "project-overview" / "write_overview.py"


def _module():
    spec = importlib.util.spec_from_file_location("write_overview", _SKILL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SKILL), "--repo", str(repo), *args], capture_output=True, text=True)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    # The repo is a subdirectory so the fixture's own scratch JSON lives
    # outside it and never lands in the history being measured.
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "keep.py").write_text("y = 1\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "a@b.c"],
        ["config", "user.name", "a"],
        ["add", "-A"],
        ["commit", "-q", "-m", "first"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    source = tmp_path / "ov.json"
    source.write_text(
        json.dumps(
            {
                "digest": "A small thing that does a small job.",
                "components": [{"path": "a.py", "role": "The whole thing."}],
                "conventions": ["Keep it small"],
            }
        ),
        encoding="utf-8",
    )
    result = _run(root, "--from", str(source))
    assert result.returncode == 0, result.stderr
    return root


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True, capture_output=True)


def test_status_on_a_fresh_overview_says_there_is_nothing_to_do(repo: Path):
    out = _run(repo, "--status").stdout
    assert "HEAD unchanged" in out
    assert "nothing to do" in out


def test_status_distinguishes_edits_from_structural_change(repo: Path):
    """The distinction the whole refresh path rests on. Editing a file's
    body cannot change what a project *is*; adding or removing files can.
    Conflating them is what makes people regenerate blindly."""
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    _commit(repo, "tweak a value")

    out = _run(repo, "--status").stdout
    assert "No files added, deleted or renamed" in out
    assert "--restamp" in out, "the cheap path must be named when it applies"
    assert "tweak a value" in out, "the commit list is what you skim to confirm"


def test_status_surfaces_added_files_and_asks_for_an_edit_not_a_rewrite(repo: Path):
    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    _commit(repo, "add a second module")

    out = _run(repo, "--status").stdout
    assert "added/deleted/renamed" in out
    assert "b.py" in out
    assert "EDIT the existing overview" in out
    assert "Only start from scratch" in out


def test_status_reports_a_deletion_too(repo: Path):
    (repo / "keep.py").unlink()
    _commit(repo, "remove keep")

    out = _run(repo, "--status").stdout
    assert "keep.py" in out
    assert "added/deleted/renamed" in out


def test_a_file_added_and_removed_again_is_not_a_structural_change(repo: Path):
    """The comparison is between endpoints, not a replay of every commit —
    correctly so. A file that appeared and vanished inside the range leaves
    the project's shape exactly as the overview already describes it."""
    (repo / "scratch.py").write_text("z = 1\n", encoding="utf-8")
    _commit(repo, "add scratch")
    (repo / "scratch.py").unlink()
    _commit(repo, "remove scratch")

    out = _run(repo, "--status").stdout
    assert "No files added, deleted or renamed" in out
    assert "--restamp" in out


def test_the_overviews_own_output_never_counts_as_structural_change(repo: Path):
    """Self-triggering guard. Where a repo doesn't gitignore .context/,
    committing the overview would otherwise make --status report "files
    were added, go update the overview" — because you updated the
    overview."""
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".context"], check=True, capture_output=True)
    _commit(repo, "commit the overview itself")

    out = _run(repo, "--status").stdout
    assert "No files added, deleted or renamed" in out
    assert "project_overview" not in out.split("Commits since:")[0]


def test_restamp_moves_the_stamp_and_changes_nothing_else(repo: Path):
    path = repo / ".context" / "project_overview.json"
    before = json.loads(path.read_text(encoding="utf-8"))

    (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
    _commit(repo, "another tweak")
    result = _run(repo, "--restamp")
    assert result.returncode == 0, result.stderr

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert after["head_sha"] != before["head_sha"]
    for key in ("digest", "components", "conventions", "entry_points"):
        assert after[key] == before[key], f"{key} must survive a restamp untouched"


def test_restamp_clears_the_apps_staleness_flag(repo: Path):
    """The point of the whole exercise: after a restamp the app should stop
    reporting drift, without anyone having re-read the repo."""
    from app.services.project_overview import overview_status

    (repo / "a.py").write_text("x = 4\n", encoding="utf-8")
    _commit(repo, "tweak")
    head = _git(repo, "rev-parse", "HEAD")
    assert overview_status(str(repo), head)["head_moved"] is True

    _run(repo, "--restamp")
    assert overview_status(str(repo), head)["head_moved"] is False


def test_restamp_rewrites_the_human_markdown_too(repo: Path):
    (repo / "a.py").write_text("x = 5\n", encoding="utf-8")
    _commit(repo, "tweak")
    _run(repo, "--restamp")

    markdown = (repo / ".context" / "project_overview.md").read_text(encoding="utf-8")
    assert _git(repo, "rev-parse", "HEAD")[:12] in markdown


def test_restamp_refuses_when_there_is_nothing_to_restamp(tmp_path: Path):
    result = _run(tmp_path, "--restamp")
    assert result.returncode != 0
    assert "no overview to restamp" in result.stderr


def test_restamp_refuses_an_overview_the_app_would_ignore(repo: Path):
    """Restamping a file that fails validation would report it as fresh in
    the settings panel while the app silently ignores it — the worst
    outcome available, because it looks fine."""
    path = repo / ".context" / "project_overview.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["digest"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")

    result = _run(repo, "--restamp")
    assert result.returncode != 0
    assert "no longer validates" in result.stderr


def test_structural_changes_ignores_pure_modifications(repo: Path):
    module = _module()
    stored = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("x = 9\n", encoding="utf-8")
    _commit(repo, "modify only")

    commits, structural = module.structural_changes(str(repo), stored)
    assert commits, "the commit should still be listed"
    assert structural == [], "a modification is not a structural change"


def test_is_derived_covers_both_sides_of_a_rename(repo: Path):
    """A rename line carries two paths and either could be the derived
    one — checking only the first would let a move out of .context/ read
    as a real structural change."""
    module = _module()
    assert module._is_derived("A\t.context/project_overview.json")
    assert module._is_derived("R100\t.briefing/x.json\tnotes/x.json")
    assert module._is_derived("R100\tnotes/x.json\t.review/x.json")
    assert not module._is_derived("A\tapp/services/call_map.py")
    assert not module._is_derived("R100\told.py\tnew.py")
