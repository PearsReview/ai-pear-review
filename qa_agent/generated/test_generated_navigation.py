"""Navigation and counting against a repo whose shape the test never knew
in advance.

Every assertion here compares something the *app* produced — the DOM, the
hunk meta line, the file tree — against the generated spec. The spec is
the oracle and is never both sides of a comparison; an assertion built
only from spec values would pass by construction and check nothing.

These are new tests, not ports. The fixed-repo suite next door keeps its
own literal assertions, which is what makes it a regression baseline.
"""

from __future__ import annotations

import pytest

from qa_agent.repo_gen import RepoSpec


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _listed_files(page) -> str:
    return " ".join(page.locator(".file-pill").all_inner_texts())


def _open_sidebar(page) -> None:
    """The file explorer starts collapsed, and a collapsed sidebar
    intercepts pointer events aimed at the pills inside it."""
    tab = page.locator("#file-sidebar-tab")
    if tab.get_attribute("aria-expanded") != "true":
        tab.click()
        page.wait_for_selector('#file-sidebar-tab[aria-expanded="true"]', timeout=5000)
        page.wait_for_timeout(400)  # let the expand transition settle


def _hunk_meta(page) -> str:
    return page.locator("#hunk-meta").inner_text()


def _walk_hunks(page, count: int) -> list[str]:
    """Every hunk's meta line, stepping with Next. Returns what was seen
    rather than asserting, so callers decide what it should mean."""
    seen = []
    for _ in range(count):
        seen.append(_hunk_meta(page))
        next_button = page.locator("#next-btn")
        if not next_button.is_enabled():
            break
        next_button.click()
        page.wait_for_timeout(400)
    return seen


def test_every_changed_file_appears_in_the_file_tree(reviewing_page, spec: RepoSpec):
    """The app decides what to list from `git diff`; the spec knows what
    was actually changed. They have to agree."""
    listed = _listed_files(reviewing_page)
    for generated in spec.changed_files:
        assert _basename(generated.path) in listed, (
            f"{generated.path} was changed but is missing from the file tree:\n{listed}"
        )


def test_unchanged_files_are_not_listed_as_changed(reviewing_page, spec: RepoSpec):
    """The mirror, and the one that actually catches something: a tree that
    listed everything would pass the test above trivially."""
    listed = _listed_files(reviewing_page)
    for generated in spec.unchanged_files:
        assert _basename(generated.path) not in listed, (
            f"{generated.path} has no diff but appears in the changed-files tree:\n{listed}"
        )


def test_the_hunk_counter_matches_the_real_diff(reviewing_page, spec: RepoSpec):
    """spec.total_hunks is measured from `git diff`, not predicted — so
    this compares the app's own hunk splitting against git's."""
    meta = _hunk_meta(reviewing_page)
    assert f"/ {spec.total_hunks}" in meta, (
        f"app reports a different total than git's {spec.total_hunks} hunks: {meta!r}"
    )


def test_stepping_through_every_hunk_never_stalls(reviewing_page, spec: RepoSpec):
    """Walks the whole change, however many hunks this seed produced. The
    fixed suite can only ever walk the four its fixture hardcodes."""
    seen = _walk_hunks(reviewing_page, spec.total_hunks)
    assert len(seen) == spec.total_hunks, (
        f"walked {len(seen)} hunks, git says there are {spec.total_hunks}:\n" + "\n".join(seen)
    )
    assert len(set(seen)) == len(seen), "the same hunk was presented twice:\n" + "\n".join(seen)


def test_a_file_with_several_hunks_presents_each_of_them(reviewing_page, spec: RepoSpec):
    """Whichever file this seed gave more than one hunk to — the fixed repo
    hardcodes which file that is."""
    multi = [f for f in spec.changed_files if f.hunk_count > 1]
    if not multi:
        pytest.skip("this seed produced no multi-hunk file")
    target = multi[0]

    seen = _walk_hunks(reviewing_page, spec.total_hunks)
    for_target = [line for line in seen if _basename(target.path) in line]
    assert len(for_target) == target.hunk_count, (
        f"{target.path} has {target.hunk_count} hunks in git but the app presented "
        f"{len(for_target)}:\n" + "\n".join(seen)
    )


def test_clicking_a_changed_file_jumps_to_it(reviewing_page, spec: RepoSpec):
    """Uses whichever changed file the seed produced, rather than a name
    baked into the test."""
    target = spec.changed_files[-1]
    name = _basename(target.path)

    _open_sidebar(reviewing_page)
    reviewing_page.locator(".file-pill", has_text=name).first.click()
    reviewing_page.wait_for_timeout(600)
    assert name in _hunk_meta(reviewing_page)


def test_the_diff_shown_contains_the_symbols_that_were_edited(reviewing_page, spec: RepoSpec):
    """Ties the rendered code view back to the actual edit. The generator
    recorded which functions it touched; walking the change must surface
    them."""
    expected = {symbol for generated in spec.changed_files for symbol in generated.changed_symbols}
    found: set[str] = set()
    for _ in range(spec.total_hunks):
        body = reviewing_page.locator("#code-pane").inner_text()
        found |= {symbol for symbol in expected if symbol in body}
        next_button = reviewing_page.locator("#next-btn")
        if not next_button.is_enabled():
            break
        next_button.click()
        reviewing_page.wait_for_timeout(400)

    assert found == expected, f"edited symbols never shown in any code view: {sorted(expected - found)}"
