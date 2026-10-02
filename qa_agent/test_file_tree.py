"""Folder collapse/expand in the file sidebar (see buildFileTree/
renderFileTreeNode in sidebar.js) — folders are real, keyboard-reachable
buttons now, and collapsing one hides everything nested under it (files
and subfolders alike) via a wrapper div toggled by sidebar.js, not by
removing/rebuilding the DOM each click.

Uses "All files" mode (see test_explore_mode.py) rather than the default
changed-files view — pkg/ (holding the never-changed unchanged.py — see
conftest.py's _seed_scratch_repo) only ever appears there; "changed files
only" mode has nothing but sample.py at the repo root, no folder to
collapse at all. pkg/ is no longer the *only* folder the scratch repo
seeds (src/ and docs/ exist too, also always zero-diff — see
_seed_scratch_repo), but it's still the one this file exercises;
`.gitignore` is *also* tracked-and-unchanged (seeded in the same initial
commit) and lives at the repo root, outside any folder — every locator
here is scoped to pkg/unchanged.py specifically, never a bare
".file-pill-unchanged", since that class now matches several files
across several folders, not just this one.
"""

from __future__ import annotations


def _pkg_folder_label(page):
    return page.locator(".file-folder-label").filter(has_text="pkg")


def _unchanged_pill(page):
    return page.locator(".file-pill-unchanged").filter(has_text="unchanged.py")


def test_folder_collapses_and_expands(page):
    page.click("#file-sidebar-tab")
    page.click("#explore-all-files-btn")
    page.wait_for_selector(".file-pill-unchanged", timeout=15000)

    folder = _pkg_folder_label(page)
    assert folder.get_attribute("aria-expanded") == "true"
    assert _unchanged_pill(page).is_visible()

    folder.click()
    assert folder.get_attribute("aria-expanded") == "false"
    assert not _unchanged_pill(page).is_visible(), "collapsing pkg/ should hide unchanged.py"

    folder.click()
    assert folder.get_attribute("aria-expanded") == "true"
    assert _unchanged_pill(page).is_visible(), "expanding again should reveal it"


def test_filter_reveals_a_match_inside_a_collapsed_folder(page):
    page.click("#file-sidebar-tab")
    page.click("#explore-all-files-btn")
    page.wait_for_selector(".file-pill-unchanged", timeout=15000)

    _pkg_folder_label(page).click()  # collapse it
    assert not _unchanged_pill(page).is_visible()

    page.fill("#file-filter-input", "unchanged")
    assert _unchanged_pill(page).is_visible(), "a search match must not stay hidden behind a collapsed folder"

    page.fill("#file-filter-input", "")
    assert not _unchanged_pill(page).is_visible(), "clearing the filter should restore the real collapsed state"
