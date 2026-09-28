"""qa_agent/repo_gen.py — the generator's own invariants.

These matter more than a normal test would, because every assertion in
qa_agent/generated/ trusts this module as its oracle. If the spec ever
describes a repo that isn't the repo on disk, a whole suite starts
reporting app bugs that aren't there.

Lives in tests/ rather than qa_agent/ deliberately: it imports repo_gen
directly and needs no browser, no server and no Ollama, so it belongs with
the fast unit tests rather than in the Playwright suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from qa_agent.repo_gen import RepoSpec, generate_repo

# A spread rather than one seed: the generator randomises structure, so a
# single seed only ever proves one shape works.
SEEDS = [0, 1, 7, 42, 99, 1729, 2026, 31337]


@pytest.fixture(scope="module")
def specs(tmp_path_factory) -> list[RepoSpec]:
    root = tmp_path_factory.mktemp("repo_gen")
    return [generate_repo(root / f"repo_{seed}", seed) for seed in SEEDS]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def test_the_same_seed_produces_the_same_repo(tmp_path: Path):
    """Reproducibility is the whole point of reporting the seed on failure.
    Without this, a seed printed by a red run is worthless."""
    first = generate_repo(tmp_path / "a", 12345)
    second = generate_repo(tmp_path / "b", 12345)

    assert [f.path for f in first.files] == [f.path for f in second.files]
    assert [f.working for f in first.files] == [f.working for f in second.files]
    assert first.total_hunks == second.total_hunks
    assert first.domain == second.domain


def test_different_seeds_produce_different_repos(tmp_path: Path):
    shapes = set()
    for seed in SEEDS:
        spec = generate_repo(tmp_path / f"r{seed}", seed)
        shapes.add((len(spec.files), len(spec.changed_files), spec.total_hunks, spec.domain))
    assert len(shapes) > 1, "every seed produced an identical shape — the generator isn't varying"


def test_hunk_counts_match_what_git_actually_reports(specs: list[RepoSpec]):
    """The core guarantee. The spec's counts are measured rather than
    predicted, and this re-derives them independently to prove it."""
    for spec in specs:
        diff = _git(spec.root, "diff", "HEAD", "--no-color", "--unified=3")
        assert diff.count("\n@@") + diff.startswith("@@") == spec.total_hunks, (
            f"seed {spec.seed}: spec says {spec.total_hunks} hunks, git diff disagrees"
        )


def test_changed_flag_agrees_with_git(specs: list[RepoSpec]):
    for spec in specs:
        reported = set(_git(spec.root, "diff", "HEAD", "--name-only").split())
        assert {f.path for f in spec.changed_files} == reported, (
            f"seed {spec.seed}: spec's changed files disagree with git's"
        )
        assert not ({f.path for f in spec.unchanged_files} & reported)


def test_every_seed_leaves_no_untracked_files(specs: list[RepoSpec]):
    """The app turns an untracked file into a synthetic whole-file hunk, so
    a stray one would silently inflate every count in the spec."""
    for spec in specs:
        untracked = _git(spec.root, "ls-files", "--others", "--exclude-standard").split()
        assert not untracked, f"seed {spec.seed} left untracked files: {untracked}"


def test_multi_hunk_files_really_got_separate_hunks(specs: list[RepoSpec]):
    """git merges edits closer than ~7 lines. The generator pads to keep
    them apart; this is what proves the padding is still enough — if
    someone shrinks _SEPARATION_LINES, this fails here rather than as a
    confusing browser-test failure."""
    checked = 0
    for spec in specs:
        for generated in spec.changed_files:
            if len(generated.changed_symbols) > 1:
                assert generated.hunk_count >= 2, (
                    f"seed {spec.seed}: {generated.path} was edited in "
                    f"{len(generated.changed_symbols)} places but git merged them"
                )
                checked += 1
    assert checked, "no seed produced a multi-hunk file — the padding path is untested"


def test_every_seed_has_a_mix_of_changed_and_unchanged_files(specs: list[RepoSpec]):
    """A file tree with nothing unchanged in it can't exercise navigation
    between the two."""
    for spec in specs:
        assert spec.changed_files, f"seed {spec.seed} has no changed files"
        assert spec.unchanged_files, f"seed {spec.seed} has no unchanged files"


def test_every_seed_produces_folders_not_just_a_flat_root(specs: list[RepoSpec]):
    for spec in specs:
        assert len([f for f in spec.folders if f]) >= 1, (
            f"seed {spec.seed} put everything at the repo root: {spec.folders}"
        )


def test_generated_sources_are_valid_python(specs: list[RepoSpec]):
    """Both sides of the diff have to parse. A file the app can't read
    would produce failures that look like app bugs."""
    import ast

    for spec in specs:
        for generated in spec.files:
            ast.parse(generated.committed)
            ast.parse(generated.working)


def test_changed_symbols_actually_appear_in_the_diff(specs: list[RepoSpec]):
    """qa_agent/generated/ asserts that edited symbols show up in the code
    view. That's only meaningful if the generator's record of what it
    edited is true."""
    for spec in specs:
        diff = _git(spec.root, "diff", "HEAD", "--no-color", "--unified=3")
        for generated in spec.changed_files:
            assert generated.changed_symbols, f"{generated.path} is changed but records no symbols"
            for symbol in generated.changed_symbols:
                assert symbol in diff, (
                    f"seed {spec.seed}: {generated.path} claims to edit {symbol}, but it doesn't appear in the diff"
                )


def test_the_guard_edit_is_placed_after_the_docstring(specs: list[RepoSpec]):
    """A guard inserted before the docstring is a real style error, and the
    LLM judges would be right to flag it — filling the findings report with
    complaints about this generator instead of about the app."""
    for spec in specs:
        for generated in spec.changed_files:
            for line_number, line in enumerate(generated.working.splitlines()):
                if line.strip().startswith("if ") and "is None" in line:
                    previous = generated.working.splitlines()[line_number - 1].strip()
                    assert previous.startswith('"""'), (
                        f"seed {spec.seed}: guard in {generated.path} follows {previous!r}, not a docstring"
                    )


def test_file_at_finds_by_path_and_reports_clearly_when_it_cannot(specs: list[RepoSpec]):
    spec = specs[0]
    assert spec.file_at(spec.files[0].path) is spec.files[0]
    with pytest.raises(KeyError):
        spec.file_at("nope/not_here.py")
