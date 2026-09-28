"""Scans a repo's Python for "who calls what" and writes .context/call_map.json.

    # what's already there, and how far HEAD has moved
    python .claude/skills/call-map/scan_calls.py --status

    # rebuild it
    python .claude/skills/call-map/scan_calls.py

    # see what it would find without writing
    python .claude/skills/call-map/scan_calls.py --dry-run

This is a scanner rather than something the model writes, unlike the
project-overview skill. The difference is what kind of fact each one
records. An overview is a judgement — what matters in this project, what
the pieces are for — and needs a reader who understands the code. A call
map is mechanical: either checkout() calls charge() or it doesn't. Asking
a model to produce mechanical facts invites confident invention, and a
fabricated caller is worse than a missing one because the review app
states it to the reviewer as background fact.

Being a script also makes it cheap to re-run, which matters: a call graph
is exactly what a refactor invalidates, so this needs to be re-runnable in
a second, not a task someone budgets for.

Python only. Definitions and calls are resolved with the `ast` module,
which is the real parser rather than a regex approximation. Other
languages are simply absent from the map, and the app treats an absent
symbol the same as a symbol with no recorded callers — it says nothing.
That is the intended failure direction: this file's whole safety argument
is that under-reporting is harmless and over-reporting is not.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Kept out of the map entirely. These are defined in nearly every class, so
# "who calls __init__" is both ambiguous and useless — and they'd be
# dropped by the ambiguity rule below anyway, at the cost of noise in the
# dropped-name report that would hide the interesting collisions.
_IGNORED_NAMES = {
    "__init__",
    "__post_init__",
    "__repr__",
    "__str__",
    "__eq__",
    "__hash__",
    "__enter__",
    "__exit__",
    "__aenter__",
    "__aexit__",
    "__call__",
    "__len__",
    "__iter__",
    "__next__",
    "__getitem__",
    "__setitem__",
    "__contains__",
    "main",
    "setup",
    "teardown",
}

MAX_CALLERS_STORED = 20
# Enough to name where a symbol is tested; the count carries the rest.
MAX_TEST_FILES_STORED = 5
MODULE_LEVEL = "<module-level>"


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


def call_map_path(repo: str) -> Path:
    return Path(repo) / ".context" / "call_map.json"


def markdown_path(repo: str) -> Path:
    return Path(repo) / ".context" / "call_map.md"


def python_files(repo: str) -> list[str]:
    """Tracked plus untracked-but-not-ignored .py files.

    Via git rather than a glob so .gitignore is honoured for free — a
    plain rglob would walk .venv/ and site-packages and produce a map of
    somebody else's library.
    """
    result = subprocess.run(
        ["git", "-C", repo, "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise SystemExit(f"git ls-files failed in {repo}: {result.stderr.strip()}")
    return sorted(line for line in result.stdout.splitlines() if line.endswith(".py"))


class _Scanner(ast.NodeVisitor):
    """Collects definitions and call sites from one module.

    Tracks a stack of enclosing function names so each call can be
    attributed to whatever function it sits in; calls outside any function
    are attributed to MODULE_LEVEL, which is a genuine caller — import-time
    wiring is how a lot of this app's own setup happens.
    """

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.definitions: list[tuple[str, str, int]] = []  # (name, kind, lineno)
        self.calls: list[tuple[str, str]] = []  # (callee_name, enclosing_name)
        self._stack: list[str] = []

    def _visit_def(self, node, kind: str) -> None:
        self.definitions.append((node.name, kind, node.lineno))
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    # The CapWords method names below are ast.NodeVisitor's dispatch API -
    # it looks up visit_<ClassName> - not a naming choice of ours.
    def visit_FunctionDef(self, node):
        self._visit_def(node, "function")

    def visit_AsyncFunctionDef(self, node):
        self._visit_def(node, "function")

    def visit_ClassDef(self, node):
        self._visit_def(node, "class")

    def visit_Call(self, node):
        callee = _callee_name(node.func)
        if callee:
            self.calls.append((callee, self._stack[-1] if self._stack else MODULE_LEVEL))
        self.generic_visit(node)


def _callee_name(func: ast.expr) -> str | None:
    """The bare name being called. `charge()` -> "charge",
    `self.charge()` / `billing.charge()` -> "charge".

    Deliberately drops the receiver. Resolving `self`/`billing` to an
    actual type needs real inference, and guessing would produce exactly
    the confident-but-wrong edges this map exists to avoid. The cost is
    ambiguity, which is handled by dropping ambiguous names outright
    rather than by picking a likely answer.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def scan(repo: str) -> tuple[list[dict], dict]:
    """Returns (symbols, report). Report carries the counts worth showing a
    human — especially how many names were dropped as ambiguous, since
    that number silently bounds how useful the map can be."""
    files = python_files(repo)
    definitions: list[tuple[str, str, str, int]] = []  # (name, file, kind, lineno)
    calls: list[tuple[str, str, str]] = []  # (callee, caller_name, caller_file)
    unparsed: list[str] = []

    for rel_path in files:
        try:
            source = (Path(repo) / rel_path).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError) as exc:
            # A file that doesn't parse is skipped, not fatal: a repo mid-edit
            # routinely has one, and refusing to produce a map because of it
            # would make this unusable exactly when it's most wanted.
            unparsed.append(f"{rel_path}: {exc}")
            continue
        scanner = _Scanner(rel_path)
        scanner.visit(tree)
        for name, kind, lineno in scanner.definitions:
            definitions.append((name, rel_path, kind, lineno))
        for callee, enclosing in scanner.calls:
            calls.append((callee, enclosing, rel_path))

    # A name defined in more than one place cannot be attributed by name
    # alone — a call to charge() might reach either definition, and this
    # scanner has no way to tell. Dropped rather than guessed: the app
    # presents these as fact to the reviewer.
    defined_count = collections.Counter(name for name, _, _, _ in definitions)
    ambiguous = {name for name, count in defined_count.items() if count > 1}

    callers_by_name: dict[str, list[dict]] = collections.defaultdict(list)
    for callee, caller_name, caller_file in calls:
        if callee in ambiguous or callee in _IGNORED_NAMES:
            continue
        callers_by_name[callee].append({"name": caller_name, "file": caller_file})

    symbols = []
    for name, rel_path, kind, lineno in sorted(definitions):
        if name in ambiguous or name in _IGNORED_NAMES:
            continue
        callers = _dedupe(callers_by_name.get(name, []), own_name=name, own_file=rel_path)
        if not callers:
            # No entry at all rather than an entry with an empty list. The
            # app never says "nothing calls this" (absence here means the
            # scan found nothing, not that nothing exists), so storing it
            # would only inflate the file.
            continue
        # Counted from the full list, before the cap below: callers are sorted
        # production-first, so the cap is exactly what drops test callers,
        # and "is this tested?" must not depend on how popular a symbol is.
        test_files = sorted({c["file"] for c in callers if _looks_like_test(c["file"])})
        symbols.append(
            {
                "name": name,
                "file": rel_path,
                "kind": kind,
                "line": lineno,  # for a human reading the .md; the app matches on name
                "callers": callers[:MAX_CALLERS_STORED],
                "caller_count": len(callers),
                "test_caller_count": sum(1 for c in callers if _looks_like_test(c["file"])),
                "test_files": test_files[:MAX_TEST_FILES_STORED],
            }
        )

    report = {
        "files_scanned": len(files),
        "definitions_found": len(definitions),
        "symbols_with_callers": len(symbols),
        "ambiguous_names_dropped": len(ambiguous),
        "unparsed": unparsed,
    }
    return symbols, report


def _looks_like_test(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if any(part in ("tests", "test", "spec", "__tests__", "qa_agent") for part in parts[:-1]):
        return True
    name = parts[-1]
    return name.startswith("test_") or name.startswith("conftest") or name.endswith("_test.py")


def _dedupe(callers: list[dict], own_name: str, own_file: str) -> list[dict]:
    """De-duplicated callers, production code first.

    The ordering is load-bearing, not cosmetic. Both this file and the app
    cap how many callers are kept, and a well-tested function usually has
    more test callers than real ones — so a naive sort would fill the whole
    budget with test files and hide the one production call site that tells
    the reviewer what actually depends on this. Tests are kept rather than
    dropped: "only called from tests" is itself worth knowing.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for caller in callers:
        key = (caller["name"], caller["file"])
        if key in seen:
            continue
        if caller["name"] == own_name and caller["file"] == own_file:
            continue  # recursion is not a dependency worth reporting
        seen.add(key)
        out.append(caller)
    return sorted(out, key=lambda c: (_looks_like_test(c["file"]), c["file"], c["name"]))


def render_markdown(data: dict, report: dict) -> str:
    lines = [
        "# Call map",
        "",
        f"_Generated {data['generated_at']} at `{(data.get('head_sha') or 'unknown')[:12]}`._",
        "_Written by the call-map skill for the AI Pear Review app;",
        "regenerate with the skill rather than hand-editing — the app reads the .json beside this._",
        "",
        f"Scanned {report['files_scanned']} Python files, found {report['definitions_found']} "
        f"definitions, {report['symbols_with_callers']} of which have recorded callers.",
        "",
        f"{report['ambiguous_names_dropped']} names are defined in more than one place and were "
        "dropped - a call to them can't be attributed by name alone.",
        "",
    ]
    by_file: dict[str, list[dict]] = collections.defaultdict(list)
    for symbol in data["symbols"]:
        by_file[symbol["file"]].append(symbol)
    for file_path in sorted(by_file):
        lines += [f"## `{file_path}`", ""]
        for symbol in sorted(by_file[file_path], key=lambda s: -s["caller_count"]):
            callers = ", ".join(f"`{c['name']}` ({c['file']})" for c in symbol["callers"])
            more = ""
            if symbol["caller_count"] > len(symbol["callers"]):
                more = f" _(+{symbol['caller_count'] - len(symbol['callers'])} more)_"
            lines.append(f"- **`{symbol['name']}`** (line {symbol['line']}) — called by {callers}{more}")
        lines.append("")
    if report["unparsed"]:
        lines += ["## Files that could not be parsed", ""]
        lines += [f"- {u}" for u in report["unparsed"]] + [""]
    return "\n".join(lines)


def print_status(repo: str, path: Path, current: str | None) -> None:
    if not path.exists():
        print("no call map written yet (.context/call_map.json missing)")
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"call map exists but is unreadable ({exc}) - rerun the scan")
        return
    stored = existing.get("head_sha")
    print(f"call map written {existing.get('generated_at')} at {str(stored)[:12]}")
    print(f"symbols with callers: {len(existing.get('symbols') or [])}")
    if stored and current and stored != current:
        behind = subprocess.run(
            ["git", "-C", repo, "rev-list", "--count", f"{stored}..HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        print(f"HEAD has moved since ({behind or '?'} commits) - rerun the scan, it takes a second")
    else:
        print("HEAD unchanged since it was written")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--status", action="store_true", help="report what's already written, then exit")
    parser.add_argument("--dry-run", action="store_true", help="scan and report, but write nothing")
    args = parser.parse_args()

    path = call_map_path(args.repo)
    current = head_sha(args.repo)

    if args.status:
        print_status(args.repo, path, current)
        return

    symbols, report = scan(args.repo)
    for key, value in report.items():
        if key != "unparsed":
            print(f"{key}: {value}", file=sys.stderr)
    for problem in report["unparsed"]:
        print(f"could not parse {problem}", file=sys.stderr)

    if args.dry_run:
        print("dry run - nothing written", file=sys.stderr)
        return

    if not symbols:
        raise SystemExit(
            "no symbols with callers found — refusing to write an empty map. "
            "If this repo isn't Python, the call-map skill has nothing to offer it yet."
        )

    data = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "head_sha": current,
        "language": "python",
        "symbols": symbols,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    markdown_path(args.repo).write_text(render_markdown(data, report), encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)
    print(f"wrote {markdown_path(args.repo)}", file=sys.stderr)


if __name__ == "__main__":
    main()
