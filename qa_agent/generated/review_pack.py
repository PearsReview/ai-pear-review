"""Writes a review pack: everything a human or a Claude Code session needs
to judge the app's narration quality, with no LLM in the test loop.

Why no live judge. The judge in the sibling suite is the *same* local model
that produced the text being judged (conftest's judge_model_config reads
the app's own conversation.ollama block), so it shares the blind spots of
the thing it is grading — and `judge_with_voting`'s k=3 exists mostly to
paper over how unreliable it is. Recording instead of judging costs three
model calls fewer per verdict, removes that circularity, and hands the
judgment to something that can also *fix* what it finds. A verdict written
into a JSONL drives nothing on its own.

This follows the pattern the rest of the project already uses for
expensive reasoning (.claude/skills/): cheap deterministic work happens in
the harness, the hard thinking happens in a Claude Code session, and the
two meet at a file.

The deterministic flags below are not judgments — they are the cheap,
exact checks worth doing mechanically so the reader can spend attention on
what actually needs a mind. Each one says what it saw, never whether it is
acceptable.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import json
import re
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
PACK_PATH = RESULTS_DIR / "generated_narration_pack.md"

# Two narrations this similar, for hunks in different files, is worth a
# look: the generated repo deliberately makes structurally identical edits
# in different places, so near-identical text suggests the model is
# pattern-matching the shape rather than reading the code.
_NEAR_DUPLICATE_RATIO = 0.85

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def foreign_symbols(narration: str, spec, file_path: str, diff: str) -> list[str]:
    """Symbols the narration names that live in a *different* file and do
    not appear in the diff it was shown.

    This is the check the generated repo uniquely makes possible. Against
    one fixed scratch repo you would be hardcoding "farewell"; here the
    generator knows every symbol's home file, so attributing one file's
    function to another is detectable automatically.

    Conservative on purpose — only symbols defined in exactly one file
    count, so the filler functions that appear in several modules can never
    trip it, and anything visible in the diff is fair game to mention.
    """
    homes: dict[str, set[str]] = {}
    for generated in spec.files:
        for symbol in generated.symbols:
            homes.setdefault(symbol, set()).add(generated.path)

    mentioned = set(_IDENTIFIER_RE.findall(narration))
    foreign = []
    for symbol in sorted(mentioned):
        owners = homes.get(symbol)
        if not owners or len(owners) != 1:
            continue  # unknown word, or a name that isn't unique to one file
        if file_path in owners:
            continue  # its own file — legitimate
        if symbol in diff:
            continue  # visible in what it was shown
        foreign.append(f"{symbol} (defined in {next(iter(owners))})")
    return foreign


def load_briefing(repo_root: Path, file_path: str, diff: str) -> dict | None:
    """The briefing that fed this hunk's narration, read straight off disk.

    The briefing agent is the first of two chained model calls — it writes
    notes to .briefing/*.json, and the narration agent is then handed those
    notes and speaks. Recording only the narration means a note the
    *briefing* invented looks like the narration lying, and you go and fix
    the wrong prompt. This run produced exactly that: a fabricated claim
    about `max()`'s time complexity, authored by the briefing and repeated
    faithfully downstream.

    Matched on the stored file_path + header rather than by recomputing the
    app's cache-key hash, which would duplicate briefing_service internals
    that qa_agent deliberately never imports. Free either way: the file is
    already on disk as a side effect of the run.
    """
    briefing_dir = repo_root / ".briefing"
    if not briefing_dir.is_dir():
        return None
    header = diff.strip().splitlines()[0] if diff.strip() else ""
    for path in briefing_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("file_path") == file_path and data.get("header") == header:
            return data
    return None


def briefing_flags(briefing: dict | None, narration: str) -> list[str]:
    """Notes about the briefing worth a reader's eye. Observations, never
    verdicts — whether a risk note is *justified* needs a mind."""
    if not briefing:
        return []
    flags = []
    risk = (briefing.get("risk_notes") or "").strip()
    if risk:
        # Measures the field itself, not its effect on narration. Since
        # _hunk_prompt stopped injecting risk_notes, the old "reached the
        # reviewer as fact" counter is zero by construction — which would
        # have read as "fixed" while the briefing carried on inventing
        # them. What's worth tracking now is how often the field is filled
        # at all: BRIEFING_SYSTEM asks for null on most hunks, so a high
        # rate means that instruction isn't landing.
        flags.append("briefing: wrote a risk note (the prompt asks for null unless it is concrete)")
    if risk and not _cites_the_diff(risk, briefing.get("header", "")):
        # Prep for the durable fix: a note that names no identifier from
        # the code it is about cannot be checked against anything, which is
        # exactly the shape the fabricated max() claim had.
        flags.append("briefing: the risk note names nothing from the diff — ungroundable")
    if risk and _echoes(risk, narration):
        flags.append("briefing: the narration repeats the briefing's risk note — check the note itself first")
    return flags


def _cites_the_diff(risk_note: str, header: str) -> bool:
    """Whether the note names at least one identifier that looks like it
    came from the code, rather than being pure prose.

    A weak proxy for real grounding — it cannot tell whether the identifier
    is used correctly, only that the note is *about* something nameable.
    Deliberately weak: it is a counter for a reader, not a gate.
    """
    backticked = re.findall(r"`([^`]+)`", risk_note)
    return bool(backticked)


def _echoes(risk_note: str, narration: str) -> bool:
    """Whether the narration is substantially restating the risk note.
    Compares content words, so a reworded echo still counts."""
    stop = {
        "the",
        "a",
        "an",
        "is",
        "it",
        "to",
        "of",
        "in",
        "if",
        "this",
        "that",
        "and",
        "or",
        "be",
        "as",
        "could",
        "might",
        "may",
        "with",
        "for",
        "by",
    }

    def words(text):
        return {w.lower() for w in _IDENTIFIER_RE.findall(text)} - stop

    note_words, narration_words = words(risk_note), words(narration)
    if len(note_words) < 4:
        return False
    return len(note_words & narration_words) / len(note_words) >= 0.5


def near_duplicates(narrations: list[dict]) -> list[dict]:
    """Pairs of narrations that read almost the same despite describing
    different hunks."""
    pairs = []
    for i in range(len(narrations)):
        for j in range(i + 1, len(narrations)):
            ratio = difflib.SequenceMatcher(
                None, narrations[i]["narration_text"], narrations[j]["narration_text"]
            ).ratio()
            if ratio >= _NEAR_DUPLICATE_RATIO:
                pairs.append(
                    {
                        "a": narrations[i]["file_path"],
                        "b": narrations[j]["file_path"],
                        "ratio": round(ratio, 3),
                    }
                )
    return pairs


def _count(entries: list[dict], prefix: str) -> int:
    return sum(1 for e in entries if any(f.startswith(prefix) for f in e["flags"]))


def write_pack(spec, entries: list[dict], duplicates: list[dict]) -> Path:
    """Renders the pack. `entries` carry, per hunk: file_path, diff,
    narration_text, edited_symbols, and the flags plus (when capture is on)
    the verbatim prompt the model received."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    flagged = [e for e in entries if e["flags"]]

    lines = [
        "# Generated-repo narration pack",
        "",
        f"_Recorded {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} — "
        f"seed `{spec.seed}`, domain `{spec.domain}`, {spec.total_hunks} hunks._",
        "",
        "Nothing here has been judged. These are recordings plus cheap "
        "deterministic flags, for a Claude Code session (or a human) to "
        "assess — see review_pack.py's docstring for why there is no live "
        "judge in the loop.",
        "",
        f"Reproduce this exact repo with `pytest qa_agent/generated/ --repo-seed={spec.seed}`.",
        "",
        "## At a glance",
        "",
        f"- Hunks narrated: **{len(entries)}** of {spec.total_hunks}",
        f"- Hunks with a deterministic flag: **{len(flagged)}**",
        f"- Spoken-form breaches (code fence, preamble, label): **{_count(entries, 'style:')}**",
        f"- Briefings that wrote a risk note (prompt asks for null unless concrete): "
        f"**{_count(entries, 'briefing: wrote a risk note')}** of "
        f"{sum(1 for e in entries if e.get('briefing'))} briefings",
        f"- Risk notes naming nothing from the diff: **{_count(entries, 'briefing: the risk note names nothing')}**",
        f"- Near-duplicate narration pairs: **{len(duplicates)}**",
        "",
        "These are rates, not pass/fail. The behaviour is probabilistic — "
        "three runs of one seed produced a canned preamble, then nothing, "
        "then a full diff dump — so what's worth tracking is whether a "
        "prompt change moves the number, not whether one run was clean.",
        "",
    ]

    if duplicates:
        lines += ["### Near-duplicate narrations", ""]
        for pair in duplicates:
            lines.append(f"- `{pair['a']}` vs `{pair['b']}` — {pair['ratio']:.0%} similar")
        lines.append("")

    lines += ["## What to look at", "", _GUIDANCE, ""]

    for index, entry in enumerate(entries, start=1):
        lines += [
            f"## Hunk {index} — `{entry['file_path']}`",
            "",
            f"Functions the generator edited here: "
            f"{', '.join(f'`{s}`' for s in entry['edited_symbols']) or '_none recorded_'}",
            "",
            "```diff",
            entry["diff"].strip(),
            "```",
            "",
            "**The app said:**",
            "",
            f"> {entry['narration_text'].strip()}",
            "",
        ]
        briefing = entry.get("briefing")
        if briefing:
            lines += [
                "**The briefing it was built on** "
                f"(confidence `{briefing.get('confidence')}`, source `{briefing.get('source')}`):",
                "",
                f"- _intent_: {briefing.get('intent') or '_none_'}",
            ]
            if briefing.get("alternatives_considered"):
                lines.append(f"- _alternatives_: {briefing['alternatives_considered']}")
            if briefing.get("risk_notes"):
                lines.append(f"- _risk notes_: {briefing['risk_notes']}")
            lines.append("")
        elif entry.get("prompt") and "What you actually know" not in entry["prompt"]:
            lines += ["_No briefing reached this narration (low confidence, or none cached)._", ""]

        if entry["flags"]:
            lines += ["**Deterministic flags:**", ""]
            lines += [f"- {flag}" for flag in entry["flags"]]
            lines.append("")
        if entry.get("prompt"):
            lines += [
                "<details><summary>The exact prompt the model received</summary>",
                "",
                "```",
                entry["prompt"].strip(),
                "```",
                "",
                "</details>",
                "",
            ]

    PACK_PATH.write_text("\n".join(lines), encoding="utf-8")
    (RESULTS_DIR / "generated_narration_pack.json").write_text(
        json.dumps(
            {"seed": spec.seed, "domain": spec.domain, "entries": entries, "duplicates": duplicates},
            indent=2,
        ),
        encoding="utf-8",
    )
    return PACK_PATH


_GUIDANCE = """\
For each hunk, the questions worth asking:

1. **Grounded?** Is everything asserted about the code actually visible in
   the diff? Invented behaviour, files or motivations are the failure this
   suite has caught before — a narration once explained "database query
   optimisation" for a diff that only added a `farewell()` function.
2. **Right target?** Does it describe the function the diff actually
   changed, rather than a neighbour? The `foreign symbol` flag catches the
   blatant version; subtler misattribution needs reading.
3. **Useful?** Does it say why, or only restate the diff line by line? A
   mechanical readout is technically accurate and worth nothing.
4. **Distinct?** The generated repo makes similar edits in different
   files on purpose. Narrations that vary only by name suggest the model is
   pattern-matching the diff's shape instead of reading it.
5. **Form?** Spoken text, a few sentences, no markdown or preamble. The
   `style` flags cover the unambiguous breaches.

**When a narration is wrong, check the briefing first.** Two model calls
run in a chain — the briefing agent writes notes, then the narration agent
is handed those notes and speaks. Both are shown above, so the two cases
are distinguishable rather than a guess:

- the briefing was already wrong, and narration repeated it faithfully
  -> fix `app/services/briefing_service.py` / `BRIEFING_SYSTEM` in `app/prompts/`
- the briefing was fine and narration mangled it
  -> fix the narration prompt in `app/prompts/`

The exact prompt is in each collapsed block, so "what was the model given"
is never in doubt.
"""
