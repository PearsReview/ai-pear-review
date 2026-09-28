"""Records what the app narrated for every hunk of a generated repo, with
cheap deterministic flags — no LLM judge in the loop.

Two of these tests assert; the rest only record. The split is deliberate:
a rule that is exact and mechanical (spoken form, attributing a function to
the wrong file) is worth failing on, because there is no judgment involved
and a regression is unambiguous. Everything softer goes in the pack for a
Claude Code session to assess, since a weak automated verdict is worse than
an honest recording — see review_pack.py's docstring.

What the generated repo adds over the fixed one: the sibling suite judges
the first hunk of a single repo. Here every hunk of a varied repo is
recorded, and the hunks are deliberately similar in shape while differing
in file and function — which is exactly the situation where a model starts
attributing one file's code to another.
"""

from __future__ import annotations

import pytest

from qa_agent.llm_capture import find_capture_for_response
from qa_agent.repo_gen import RepoSpec
from qa_agent.scenarios.scenario_helpers import style_violations

from .review_pack import (
    briefing_flags,
    foreign_symbols,
    load_briefing,
    near_duplicates,
    write_pack,
)


def _symbols_in_this_hunk(spec: RepoSpec, file_path: str, diff: str) -> list[str]:
    """The edited functions belonging to *this hunk*, not to its whole file.

    RepoSpec records changed_symbols per file, but a two-hunk file edits a
    different function in each hunk — so asking "did the narration name the
    edited function?" with the file's full list flags a narration that
    correctly described the one function actually in front of it. Narrowing
    by what appears in the hunk's own diff text fixes that; found by reading
    the first pack this suite produced, where two of five hunks were
    flagged purely by this mistake.
    """
    try:
        generated = spec.file_at(file_path)
    except KeyError:
        return []
    return [symbol for symbol in generated.changed_symbols if symbol in diff]


@pytest.fixture(scope="session")
def recorded(narrated_hunks, spec: RepoSpec, llm_capture_dir) -> dict:
    """Turns the raw walk into pack entries: the diff, what the app said,
    the prompt it was given, and the deterministic flags."""
    entries = []
    for pair in narrated_hunks:
        presenting, narration = pair["presenting"], pair["narration"]
        file_path = presenting["file_path"]
        diff = presenting.get("diff", "")
        text = narration["text"]

        edited = _symbols_in_this_hunk(spec, file_path, diff)

        flags = []
        for violation in style_violations(text):
            flags.append(f"style: {violation}")
        foreign = foreign_symbols(text, spec, file_path, diff)
        for symbol in foreign:
            flags.append(f"foreign symbol: names {symbol}, which is not in this hunk's file or diff")
        if edited and not any(symbol in text for symbol in edited):
            # Recorded, never asserted: a narration can be perfectly good
            # without naming the function ("this adds a guard against null
            # input" is fine). Only worth a reader's eye.
            flags.append(f"names none of the edited functions ({', '.join(edited)})")

        briefing = load_briefing(spec.root, file_path, diff)
        flags.extend(briefing_flags(briefing, text))

        capture = find_capture_for_response(llm_capture_dir, text)
        prompt = ""
        if capture and capture.get("messages"):
            prompt = capture["messages"][-1].get("content", "")

        entries.append(
            {
                "file_path": file_path,
                "diff": diff,
                "narration_text": text,
                "edited_symbols": edited,
                "foreign_symbols": foreign,
                "briefing": briefing,
                "flags": flags,
                "prompt": prompt,
            }
        )

    duplicates = near_duplicates(entries)
    path = write_pack(spec, entries, duplicates)
    print(f"\nnarration pack written to {path}")
    return {"entries": entries, "duplicates": duplicates}


def test_every_hunk_was_actually_narrated(recorded, spec: RepoSpec):
    """The walk has to be complete before anything read from it means
    much — a pack covering 2 of 5 hunks would look clean by omission."""
    assert len(recorded["entries"]) == spec.total_hunks, (
        f"narrated {len(recorded['entries'])} of {spec.total_hunks} hunks"
    )
    for entry in recorded["entries"]:
        assert entry["narration_text"].strip(), f"empty narration for {entry['file_path']}"


def test_narration_never_attributes_code_to_the_wrong_file(recorded):
    """The one hallucination check exact enough to assert on.

    A symbol that is defined in exactly one file, is not that of the hunk
    being narrated, and does not appear in the diff the model was shown,
    cannot have been read — it was recalled or invented. The fixed-repo
    suite can't test this without hardcoding function names; here the
    generator knows where every symbol lives.
    """
    offenders = {
        entry["file_path"]: entry["foreign_symbols"] for entry in recorded["entries"] if entry["foreign_symbols"]
    }
    assert not offenders, (
        f"narration named functions belonging to other files, which were not in the diff it was shown: {offenders}"
    )


def test_record_narration_form_violations(recorded):
    """Records the *rate* of spoken-form breaches rather than asserting on
    them.

    The check itself is deterministic — text either contains a code fence
    or it doesn't — but the behaviour being checked is not. Three runs of
    this same seed produced a canned preamble, then nothing, then a full
    ```diff dump: the app breaks its own form intermittently, on maybe two
    runs in three. A hard assertion on that is red most of the time for a
    reason nobody can act on in the moment, and a test that cries wolf gets
    muted — which would lose the signal entirely.

    A count per run is the useful shape: comparable across runs, so a
    prompt change can be shown to have moved it.
    """
    offenders = {
        entry["file_path"]: [f for f in entry["flags"] if f.startswith("style:")]
        for entry in recorded["entries"]
        if any(f.startswith("style:") for f in entry["flags"])
    }
    if offenders:
        print(f"\nform violations in {len(offenders)} of {len(recorded['entries'])} narrations: {offenders}")


def test_pack_records_the_prompt_behind_each_narration(recorded):
    """A recording nobody can act on is not worth writing. Without the
    prompt, "this narration is wrong" can't be traced to whether the model
    was given bad context or handled good context badly — which is the
    difference between fixing app/prompts/ and fixing nothing."""
    missing = [e["file_path"] for e in recorded["entries"] if not e["prompt"]]
    assert not missing, (
        f"no captured prompt for {missing} — is debug.capture_llm_calls still "
        "being patched on in this suite's conftest?"
    )


def test_pack_records_the_briefing_behind_each_narration(recorded, spec: RepoSpec):
    """The briefing is the first of two chained model calls, and this run's
    one real defect was authored there and merely repeated downstream.
    Without it in the pack, that case is indistinguishable from the
    narration inventing things itself — so a silently-empty briefing
    section would quietly send every future diagnosis to the wrong prompt.

    Not every hunk gets one (the cache is written per hunk as narration
    runs), so this asserts the mechanism works at all rather than demanding
    a briefing everywhere.
    """
    if not (spec.root / ".briefing").is_dir():
        pytest.skip("no briefings were cached during this run")
    found = [e for e in recorded["entries"] if e.get("briefing")]
    assert found, (
        "briefings exist on disk but none matched a narrated hunk — the "
        "file_path/header match in load_briefing has probably drifted from "
        "what briefing_service.py writes"
    )


def test_record_narration_variety(recorded):
    """Records only. The generated repo makes structurally similar edits in
    different files on purpose, so near-identical narrations suggest the
    model is pattern-matching the diff's shape rather than reading it —
    but "too similar" is a judgment, and similar edits legitimately produce
    similar descriptions. Flagged in the pack, not failed here."""
    duplicates = recorded["duplicates"]
    if duplicates:
        print(f"\n{len(duplicates)} near-duplicate narration pair(s) recorded for review:")
        for pair in duplicates:
            print(f"  {pair['a']} vs {pair['b']} — {pair['ratio']:.0%} similar")
