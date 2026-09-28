"""Generates a throwaway git repo with a varied, seeded diff, plus a spec
describing exactly what it contains.

Why this exists. The rest of this suite runs against one hardcoded scratch
repo, which means every test only ever sees one shape of change: one
domain's vocabulary, one folder layout, one distribution of hunks. Tests
written against that quietly bake in its particulars, and the app's own
assumptions never get poked from a different angle.

This is deliberately NOT a replacement for that fixed repo. The two do
different jobs: the fixed repo is a regression baseline where a failure
means the app broke, and this is a robustness probe where a failure might
also mean a test made an assumption nobody noticed. Mixing them makes
every red run ambiguous, so they stay separate (see qa_agent/generated/).

The important design decision here is that **hunk counts are measured, not
predicted**. `git diff --unified=3` merges edits that land within about
seven lines of each other, and the exact threshold depends on the
surrounding content — so a generator that computed "this file has 2 hunks"
from its own intentions would be confidently wrong some fraction of the
time, and every test trusting the spec would fail in a way that looks like
an app bug. Instead this generates, commits, modifies, then runs the real
`git diff` and counts what actually came out. The spec is true by
construction.

No untracked files are ever left behind: the app presents an untracked
file as a synthetic whole-file hunk, which would make the counts here
depend on a code path this generator isn't trying to exercise.
"""

from __future__ import annotations

import random
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Enough unchanged lines between two intended-separate edits that git won't
# merge them into one hunk. git diff --unified=3 shows 3 lines of context
# either side, so edits with 7+ lines between them stay separate; this
# leaves real headroom rather than sitting on the boundary. The generator
# still verifies the outcome instead of trusting this number.
_SEPARATION_LINES = 14

_MIN_HUNKS = 2
_MIN_CHANGED_FILES = 2


@dataclass
class GeneratedFile:
    path: str  # posix, relative to the repo root
    committed: str  # content at HEAD
    working: str  # content in the working tree (== committed when unchanged)
    symbols: list[str]  # function names defined in this file
    changed_symbols: list[str] = field(default_factory=list)  # those whose body a hunk touches
    hunk_count: int = 0  # measured from the real git diff, never predicted

    @property
    def changed(self) -> bool:
        return self.working != self.committed


@dataclass
class RepoSpec:
    """Everything a test would otherwise hardcode about the repo.

    Anything a test asserts should come from here rather than from a
    literal — but see the note in qa_agent/generated/conftest.py about not
    letting that turn assertions tautological.
    """

    root: Path
    seed: int
    domain: str
    files: list[GeneratedFile]

    @property
    def changed_files(self) -> list[GeneratedFile]:
        return [f for f in self.files if f.changed]

    @property
    def unchanged_files(self) -> list[GeneratedFile]:
        return [f for f in self.files if not f.changed]

    @property
    def total_hunks(self) -> int:
        return sum(f.hunk_count for f in self.files)

    @property
    def folders(self) -> list[str]:
        """Distinct directories containing files, "" for the repo root."""
        return sorted({str(Path(f.path).parent).replace("\\", "/").replace(".", "") for f in self.files})

    def file_at(self, path: str) -> GeneratedFile:
        for candidate in self.files:
            if candidate.path == path:
                return candidate
        raise KeyError(f"{path} is not in this spec: {[f.path for f in self.files]}")

    def describe(self) -> str:
        lines = [f"seed={self.seed} domain={self.domain} hunks={self.total_hunks}"]
        for f in self.files:
            state = f"{f.hunk_count} hunk(s)" if f.changed else "unchanged"
            lines.append(f"  {f.path:32} {state}")
        return "\n".join(lines)


# Vocabulary per domain. Names carry meaning on purpose: the LLM judges in
# this suite score whether a narration is grounded and sensible, and a repo
# full of fn_1/fn_2 gives them nothing to be right or wrong about.
_DOMAINS = [
    {
        "name": "inventory",
        "package": "stock",
        "modules": ["warehouse", "reorder", "barcode", "shipment"],
        "nouns": ["item", "batch", "pallet", "sku"],
        "verbs": ["count", "reserve", "release", "audit"],
    },
    {
        "name": "billing",
        "package": "payments",
        "modules": ["invoice", "ledger", "refund", "tax"],
        "nouns": ["charge", "credit", "line_item", "account"],
        "verbs": ["settle", "void", "apply", "reconcile"],
    },
    {
        "name": "scheduling",
        "package": "calendar",
        "modules": ["booking", "availability", "reminder", "timezone"],
        "nouns": ["slot", "appointment", "window", "attendee"],
        "verbs": ["book", "cancel", "shift", "confirm"],
    },
    {
        "name": "telemetry",
        "package": "metrics",
        "modules": ["collector", "rollup", "alerting", "sampling"],
        "nouns": ["sample", "series", "threshold", "bucket"],
        "verbs": ["record", "flush", "aggregate", "prune"],
    },
]


def _function(name: str, noun: str, body: list[str]) -> str:
    return "\n".join([f"def {name}({noun}):", *[f"    {line}" for line in body]])


def _filler(index: int) -> str:
    """A function that exists only to hold two edits apart. Still readable
    code — a reviewer (or a judge) looking at surrounding context should
    not see obvious padding."""
    return _function(
        f"_describe_step_{index}",
        "value",
        [f'"""Step {index} of the pipeline."""', "label = str(value)", "return label.strip()"],
    )


def _module_source(rng: random.Random, domain: dict, function_count: int) -> tuple[str, list[str]]:
    """Source for one module, plus the names of its real (non-filler)
    functions, in order."""
    verbs = rng.sample(domain["verbs"], k=min(function_count, len(domain["verbs"])))
    while len(verbs) < function_count:
        verbs.append(f"{rng.choice(domain['verbs'])}_{len(verbs)}")
    nouns = domain["nouns"]

    blocks: list[str] = []
    names: list[str] = []
    for position, verb in enumerate(verbs):
        noun = nouns[position % len(nouns)]
        name = f"{verb}_{noun}"
        names.append(name)
        blocks.append(
            _function(
                name,
                noun,
                [
                    f'"""{verb.capitalize()} one {noun}."""',
                    f"total = len(str({noun}))",
                    "return total",
                ],
            )
        )
    return "\n\n\n".join(blocks) + "\n", names


def _pad_between(source: str, after_function: str) -> str:
    """Insert filler functions after the named function, so a later edit
    further down the file lands far enough away to stay its own hunk."""
    blocks = source.rstrip("\n").split("\n\n\n")
    out: list[str] = []
    for block in blocks:
        out.append(block)
        if block.startswith(f"def {after_function}("):
            needed = _SEPARATION_LINES
            index = 0
            while needed > 0:
                filler = _filler(index)
                out.append(filler)
                needed -= len(filler.splitlines()) + 2  # +2 for the blank lines between blocks
                index += 1
    return "\n\n\n".join(out) + "\n"


def _edit_guard(source: str, function_name: str, noun: str) -> str:
    """Add an input guard at the top of a function body, after the
    docstring.

    Position matters. Inserting before the docstring is a real (if small)
    style error, and the LLM judges in this suite would be right to flag
    it — which would fill the findings report with complaints about this
    generator rather than about the app under test.
    """
    pattern = re.compile(rf'(def {re.escape(function_name)}\({re.escape(noun)}\):\n    """[^\n]*"""\n)')
    guard = f'    if {noun} is None:\n        raise ValueError("{noun} is required")\n'
    patched, count = pattern.subn(rf"\1{guard}", source, count=1)
    if count != 1:
        raise AssertionError(f"could not place a guard in {function_name} — generated shape changed")
    return patched


def _edit_return(source: str, function_name: str) -> str:
    """Change what a function returns — a semantic change with no new
    lines, which is a different diff shape from an insertion."""
    marker = f"def {function_name}("
    start = source.index(marker)
    end = source.find("\n\n\n", start)
    end = len(source) if end == -1 else end
    body = source[start:end]
    return source[:start] + body.replace("return total", "return max(total, 1)", 1) + source[end:]


def generate_repo(root: Path, seed: int) -> RepoSpec:
    """Create a seeded git repo under `root` and return a truthful spec.

    Same seed, same repo — so a failing run is reproducible from the seed
    printed in the report.
    """
    rng = random.Random(seed)
    domain = rng.choice(_DOMAINS)
    package = domain["package"]

    root.mkdir(parents=True, exist_ok=True)

    # Structure varies, not just vocabulary. A generator that only renamed
    # things would still hand every test the same 4-file, 3-hunk shape, and
    # an assumption about *shape* is exactly the kind this suite exists to
    # catch. File count, which files change, how many hunks each gets, and
    # where they sit in the tree are all drawn per seed.
    file_count = rng.randint(4, 6)
    folders = ["", package, package, "docs", f"{package}/core", "tools"]
    module_names = _module_names(rng, domain, file_count)

    files: list[GeneratedFile] = []
    for index in range(file_count):
        folder = folders[index % len(folders)]
        path = f"{folder}/{module_names[index]}.py" if folder else f"{module_names[index]}.py"
        # 3+ functions so a file is *eligible* to carry two separated hunks.
        source, names = _module_source(rng, domain, rng.randint(3, 4))
        files.append(GeneratedFile(path=path, committed=source, working=source, symbols=names))

    rng.shuffle(files)
    changed_count = rng.randint(_MIN_CHANGED_FILES, min(3, file_count - 1))
    for position, generated in enumerate(files[:changed_count]):
        # At least one file always gets two hunks, so multi-hunk navigation
        # is exercised on every seed rather than most of them.
        wants_two = position == 0 or rng.random() < 0.4
        if wants_two and len(generated.symbols) >= 3:
            _apply_two_hunk_edit(generated)
        else:
            _apply_one_hunk_edit(generated)

    files.sort(key=lambda f: f.path)
    _write_and_commit(root, files)
    _apply_working_changes(root, files)
    _measure_hunks(root, files)

    spec = RepoSpec(root=root, seed=seed, domain=domain["name"], files=files)
    _validate(spec)
    return spec


def _module_names(rng: random.Random, domain: dict, count: int) -> list[str]:
    """Distinct module names, extending the domain's vocabulary with
    suffixes if more are needed than it lists."""
    names = rng.sample(domain["modules"], k=min(count, len(domain["modules"])))
    while len(names) < count:
        names.append(f"{rng.choice(domain['modules'])}_{len(names)}")
    return names


def _apply_two_hunk_edit(generated: GeneratedFile) -> None:
    """Edit the first and last functions, with filler padded between so git
    keeps them as separate hunks. The outcome is still verified by
    _measure_hunks rather than assumed."""
    padded = _pad_between(generated.committed, generated.symbols[0])
    generated.committed = padded
    first, last = generated.symbols[0], generated.symbols[-1]
    generated.working = _edit_return(_edit_guard(padded, first, _noun_of(padded, first)), last)
    generated.changed_symbols = [first, last]


def _apply_one_hunk_edit(generated: GeneratedFile) -> None:
    first = generated.symbols[0]
    generated.working = _edit_guard(generated.committed, first, _noun_of(generated.committed, first))
    generated.changed_symbols = [first]


def _noun_of(source: str, function_name: str) -> str:
    match = re.search(rf"def {re.escape(function_name)}\((\w+)\)", source)
    if not match:
        raise AssertionError(f"could not find {function_name}'s parameter in generated source")
    return match.group(1)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout


def _write_and_commit(root: Path, files: list[GeneratedFile]) -> None:
    for generated in files:
        target = root / generated.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated.committed, encoding="utf-8")
    # Mirrors the real project's .gitignore. Without it, the app's own
    # .review/ and .briefing/ output would appear as untracked files, and
    # the app presents each untracked file as a synthetic whole-file hunk —
    # which would silently change total_hunks partway through a run.
    (root / ".gitignore").write_text(".review/\n.briefing/\n.context/\n", encoding="utf-8")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "qa-agent@example.com")
    _git(root, "config", "user.name", "QA Agent")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


def _apply_working_changes(root: Path, files: list[GeneratedFile]) -> None:
    for generated in files:
        if generated.changed:
            (root / generated.path).write_text(generated.working, encoding="utf-8")


def _measure_hunks(root: Path, files: list[GeneratedFile]) -> None:
    """Count the hunks git actually produced, per file.

    The whole reason the spec can be trusted. Parses `git diff HEAD`'s own
    output rather than reimplementing the app's splitting: one "@@" line is
    one hunk, and with no untracked files in play (see module docstring)
    that matches what the app will present exactly.
    """
    diff = _git(root, "diff", "HEAD", "--no-color", "--unified=3")
    counts: dict[str, int] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :].strip()
            counts.setdefault(current, 0)
        elif line.startswith("@@") and current is not None:
            counts[current] += 1
    for generated in files:
        generated.hunk_count = counts.get(generated.path, 0)


def _validate(spec: RepoSpec) -> None:
    """Fail loudly here rather than confusingly downstream.

    A generated repo with no hunks makes the app show "no changes found",
    which makes Start Review never appear, which makes every test in the
    session time out one by one with no indication that the repo was the
    problem. Better to never hand that out.
    """
    problems = []
    if spec.total_hunks < _MIN_HUNKS:
        problems.append(f"only {spec.total_hunks} hunk(s), need >= {_MIN_HUNKS}")
    if len(spec.changed_files) < _MIN_CHANGED_FILES:
        problems.append(f"only {len(spec.changed_files)} changed file(s), need >= {_MIN_CHANGED_FILES}")
    if not spec.unchanged_files:
        problems.append("no unchanged files — the file tree needs a mix to be worth navigating")

    for generated in spec.changed_files:
        if generated.hunk_count == 0:
            problems.append(f"{generated.path} differs on disk but git reports no hunks")
    for generated in spec.unchanged_files:
        if generated.hunk_count:
            problems.append(f"{generated.path} is unchanged but git reports {generated.hunk_count} hunks")

    # The separation this generator goes to trouble to arrange. If git
    # merged the two edits after all, say so here rather than letting a
    # test that expects two hunks fail somewhere far away.
    multi = [f for f in spec.changed_files if len(f.changed_symbols) > 1]
    for generated in multi:
        if generated.hunk_count < 2:
            problems.append(
                f"{generated.path} was edited in {len(generated.changed_symbols)} places but git "
                f"merged them into {generated.hunk_count} hunk(s) — _SEPARATION_LINES is too small"
            )

    if problems:
        raise AssertionError(
            f"generated repo (seed={spec.seed}) is unusable:\n  " + "\n  ".join(problems) + f"\n{spec.describe()}"
        )
