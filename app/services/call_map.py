"""Reads the call map a prep skill left behind, and slices out the one
fact a hunk's narration can't get from the diff: who calls the code being
changed.

Why callers specifically. The model sees a hunk and nothing around it, so
it can describe *what* changed but has no way to reason about what depends
on it — which is the first thing a human reviewer asks. Callees are the
weaker half of the same graph: a hunk that calls something usually shows
that call right there in the diff, and "where is this defined" is already
answered interactively and for free by code_search.py's Step Into. So only
the upstream direction is injected here.

Sliced by symbol, not by file. project_overview.py can key on the file
path because a project's directory layout is stable; a call graph's useful
unit is the function, and which functions a hunk touches changes hunk to
hunk within the same file.

Matched by name rather than by line number, deliberately. Line numbers are
the fastest-rotting thing in this file — every edit above a function moves
it, so a map keyed on lines would be wrong within minutes of being written
while still looking authoritative. Names survive everything except an
actual rename, and a rename shows up as a miss (nothing injected), which is
the safe direction to fail in.

Stored in .context/ alongside the overview, for the same reason: it costs
real work to produce and prune_briefing_cache() prunes .briefing/ on
every Refresh Diff.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_CONTEXT_DIR = ".context"
_CALL_MAP_FILENAME = "call_map.json"

# Budget caps. Same reasoning as project_overview.py's: the conversation
# model runs on a few thousand tokens total and everything here comes out
# of what the diff itself gets. A hunk touching more than a handful of
# symbols is one where the call map isn't the interesting part anyway.
_MAX_SYMBOLS = 6
_MAX_CALLERS_PER_SYMBOL = 5
_MAX_BLOCK_CHARS = 700


def call_map_path(repo_path: str) -> Path:
    return Path(repo_path) / _CONTEXT_DIR / _CALL_MAP_FILENAME


def load_call_map(repo_path: str) -> dict | None:
    """The stored call map, or None if absent/unreadable/empty. Absence is
    the normal case — the skill is optional and everything downstream
    behaves exactly as it did before call maps existed."""
    path = call_map_path(repo_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.info("Ignoring unreadable call map at %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
        return None
    if not data["symbols"]:
        return None  # a skeleton with nothing in it is not usable
    return data


def call_map_prompt_block(repo_path: str, file_path: str, hunk_text: str, scale: int = 1) -> str | None:
    """Callers of the symbols this hunk actually touches, or None when
    there's nothing useful to say.

    A symbol counts as touched when the map records it as defined in this
    hunk's file AND its name appears somewhere in the hunk text — including
    the unchanged context lines, since a hunk sitting inside a function
    body is exactly the case where that function's callers matter.

    scale raises the caps for a provider that manages its own window — see
    context_scale in app/web/context.py.
    """
    call_map = load_call_map(repo_path)
    if call_map is None:
        return None

    normalized_file = file_path.replace("\\", "/")
    lines: list[str] = []
    for symbol in call_map["symbols"]:
        if len(lines) >= _MAX_SYMBOLS * scale:
            break
        entry = _symbol_line(symbol, normalized_file, hunk_text)
        if entry:
            lines.append(entry)

    if not lines:
        return None

    block = "\n".join(
        [
            f"Who calls the code in {normalized_file} (from a recorded call map — this is not visible in the diff):",
            *lines,
            "The map is built by scanning and may be incomplete, so treat it as a lead to mention, not proof.",
        ]
    )
    return block[: _MAX_BLOCK_CHARS * scale]


def coverage_prompt_block(repo_path: str, file_path: str, hunk_text: str) -> str | None:
    """Which tests directly call the symbols this hunk touches, for a
    reviewer asking "is this tested?" (see app/web/question_context.py) —
    not part of every narration.

    Absence is stated only in the narrow form the scan can support: "no
    direct test calls were found", with the indirect route named. Much of
    a web app is tested only through a browser suite that never calls a
    Python function by name, so "untested" would often be false. A map
    written before these fields existed says nothing at all — a missing
    count is not a zero."""
    call_map = load_call_map(repo_path)
    if call_map is None:
        return None
    normalized_file = file_path.replace("\\", "/")
    lines: list[str] = []
    for symbol in call_map["symbols"]:
        if len(lines) >= _MAX_SYMBOLS:
            break
        if not isinstance(symbol, dict) or not isinstance(symbol.get("test_caller_count"), int):
            continue
        name = symbol.get("name")
        if not isinstance(name, str) or str(symbol.get("file", "")).replace("\\", "/") != normalized_file:
            continue
        if not _mentions(hunk_text, name):
            continue
        count = symbol["test_caller_count"]
        files = [f for f in symbol.get("test_files") or [] if isinstance(f, str)]
        if count:
            where = f" ({', '.join(files[:3])}{', …' if len(files) > 3 else ''})" if files else ""
            lines.append(f"- {name} is called directly by {count} test call site(s){where}")
        else:
            lines.append(f"- no direct test calls to {name} were found")
    if not lines:
        return None
    block = "\n".join(
        [
            f"Test coverage for {normalized_file} (from the recorded call map, direct calls only):",
            *lines,
            "Tests that exercise code indirectly (a browser or end-to-end suite) don't appear here.",
        ]
    )
    return block[:_MAX_BLOCK_CHARS]


def _symbol_line(symbol: object, normalized_file: str, hunk_text: str) -> str | None:
    if not isinstance(symbol, dict):
        return None
    name = symbol.get("name")
    defined_in = str(symbol.get("file", "")).replace("\\", "/")
    if not name or not isinstance(name, str) or defined_in != normalized_file:
        return None
    if not _mentions(hunk_text, name):
        return None

    callers = _caller_names(symbol, own_name=name, own_file=normalized_file)
    # Silence, never "nothing calls this". An empty caller list in a
    # scanned map means the scan found nothing, which is not the same as
    # nothing existing — dynamic dispatch, string-based lookup, an
    # entry point wired up in config, or simply a gap in the scanner all
    # look identical here. The model would state "unused" as fact, and a
    # reviewer could delete live code on the strength of it.
    if not callers:
        return None

    shown = callers[:_MAX_CALLERS_PER_SYMBOL]
    suffix = f", and {len(callers) - len(shown)} more" if len(callers) > len(shown) else ""
    return f"- {name} is called by: {', '.join(shown)}{suffix}"


def _caller_names(symbol: dict, own_name: str, own_file: str) -> list[str]:
    """Formatted "name (file)" caller labels, de-duplicated and with the
    symbol's own recursive calls dropped — "charge is called by charge" is
    noise that costs budget and tells the reviewer nothing."""
    callers = symbol.get("callers")
    if not isinstance(callers, list):
        return []

    seen: set[str] = set()
    names: list[str] = []
    for caller in callers:
        if not isinstance(caller, dict):
            continue
        caller_name = caller.get("name")
        if not caller_name or not isinstance(caller_name, str):
            continue
        caller_file = str(caller.get("file", "")).replace("\\", "/")
        if caller_name == own_name and caller_file in ("", own_file):
            continue
        label = f"{caller_name} ({caller_file})" if caller_file else caller_name
        if label not in seen:
            seen.add(label)
            names.append(label)
    return names


def _mentions(hunk_text: str, name: str) -> bool:
    """Whole-word match. re.escape because this name comes from a
    skill-written file the app doesn't control — an entry containing regex
    metacharacters should simply not match, not raise out of a narration."""
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", hunk_text) is not None


def call_map_status(repo_path: str, head_sha: str | None) -> dict:
    """Whether a call map exists and how far the repo has moved since it
    was written — for reporting, never for gating.

    Drift matters more here than for an overview. A project's shape
    survives a refactor; its call graph is what a refactor changes. Hence
    reported, but still not grounds for discarding: a map that's mostly
    right is worth more than no map, and the name-based matching above
    already fails quietly on the parts that moved.
    """
    call_map = load_call_map(repo_path)
    if call_map is None:
        return {"present": False}
    stored_sha = call_map.get("head_sha")
    return {
        "present": True,
        "generated_at": call_map.get("generated_at"),
        "head_sha": stored_sha,
        "current_head_sha": head_sha,
        "head_moved": bool(stored_sha and head_sha and stored_sha != head_sha),
        "symbol_count": len(call_map["symbols"]),
    }
