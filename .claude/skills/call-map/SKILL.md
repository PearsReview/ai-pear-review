---
name: call-map
description: Scan a repo's Python and write .context/call_map.{json,md} — which functions call which — so the AI Pear Review app can tell the reviewer who depends on the code in a hunk. Fast and deterministic; rerun whenever HEAD has moved. Use when asked to build a call map, function tree, or "who calls what", or to refresh the review app's context.
---

# call-map

Record who calls what, so the review app can answer the question a diff
never shows: **what else depends on this?**

## Why this exists

The app narrates one hunk at a time with a small local model that sees the
hunk's diff and nothing else. It can describe *what* changed. It has no
way to know that the function being changed is called from four other
places — which is the first thing a human reviewer wants to know, and the
difference between "a validation check was added" and "a validation check
was added to the one function every payment path goes through".

## This one is a script, not a writing task

Unlike **project-overview**, you are not authoring this. Run the scanner:

```bash
python .claude/skills/call-map/scan_calls.py
```

That writes `.context/call_map.json` (what the app reads) and
`.context/call_map.md` (what a human reads), stamped with the current HEAD.

The split is deliberate. An overview is a judgement about what matters and
needs someone who understands the code. A call map is mechanical — either
`checkout()` calls `charge()` or it doesn't. Asking a model to produce
mechanical facts invites confident invention, and a **fabricated caller is
worse than a missing one**, because the app hands it to the reviewer as
background fact.

Being a script also makes it cheap to rerun, which is the point: a call
graph is precisely what a refactor invalidates.

## What to do

1. **Check the state:**
   ```bash
   python .claude/skills/call-map/scan_calls.py --status
   ```
   Reports when it was written and how many commits HEAD has moved since.
   Unlike an overview — which describes a project's stable shape and
   survives ordinary commits — a call map should be rerun whenever HEAD has
   moved at all. It costs a second.

2. **Rerun it** (`scan_calls.py`, no flags). Use `--dry-run` first if you
   want to see the counts without writing.

3. **Read what it reports.** It prints `files_scanned`,
   `definitions_found`, `symbols_with_callers`, `ambiguous_names_dropped`,
   and any file it couldn't parse. Two things are worth your attention:

   - **A file that failed to parse** is skipped silently in the map. If
     it's a file the reviewer is about to work in, say so — that's a real
     gap in their context, and often a syntax error worth fixing anyway.
   - **A high `ambiguous_names_dropped`** bounds how useful the map can
     be. See below.

4. **Tell the user what changed** if anything notable did — a function
   that gained or lost callers is exactly the kind of thing worth
   mentioning before a review.

## What the app actually injects

Per narration, for the hunk's file, the app injects **callers of the
symbols whose names appear in that hunk** — up to 6 symbols, 5 callers
each:

```
Who calls the code in app/services/billing.py (from a recorded call map —
this is not visible in the diff):
- charge is called by: checkout (app/flow.py), refund (app/refunds.py)
The map is built by scanning and may be incomplete, so treat it as a lead
to mention, not proof.
```

When the reviewer asks whether the code is tested, the reply also gets
`test_caller_count` and `test_files` for the same symbols — counted before
the caller cap, which otherwise drops test callers first:

```
Test coverage for app/services/billing.py (from the recorded call map, direct calls only):
- charge is called directly by 2 test call site(s) (tests/test_billing.py)
Tests that exercise code indirectly (a browser or end-to-end suite) don't appear here.
```

Nothing else in the file reaches the model. `line`, `caller_count` and the
whole `.md` are for humans. A map written before the coverage fields
existed simply gets no coverage line; re-run the scan to add them.

## The two limits, and why they're the safe kind

**Ambiguous names are dropped.** The scanner resolves `self.charge()` and
`billing.charge()` to the bare name `charge`, because resolving the
receiver to a real type needs type inference this doesn't have. So when a
name is defined in more than one place, a call to it can't be attributed —
and it's dropped rather than guessed. `--status` and the `.md` both report
how many.

**Python only.** Definitions and calls come from the `ast` module, a real
parser rather than a regex approximation. Other languages are simply
absent. For this repo that means `static/js/` is not covered.

Both limits under-report, and that is the intended direction. The app
**never says "nothing calls this"** — an empty result means the scan found
nothing, which is not the same as nothing existing (dynamic dispatch, a
string-keyed lookup, an entry point wired up in config, or a plain gap in
the scanner all look identical). A reviewer could delete live code on the
strength of a confident "unused", so that sentence is never produced.

## When to run it

- **Before a review session**, alongside project-overview and prep-review.
- **After any refactor that moves or renames functions** — this is the one
  that actually invalidates the map.
- **When `--status` says HEAD has moved.** The reviewer can see this
  themselves in the app's settings panel, which shows the age of both
  `.context/` files and the skill to run.

## Notes

- Output goes to `.context/`, deliberately not `.briefing/` — the app
  prunes entries there that no longer match the diff on every Refresh Diff
  (`prune_briefing_cache`).
- A missing, empty, or malformed call map is ignored and narration carries
  on exactly as before — see `tests/test_call_map.py`.
- The scanner refuses to write an empty map rather than leaving a file
  that looks present but says nothing.
- Related: **project-overview** explains what the project is,
  **prep-review** explains each hunk, this one explains what depends on
  what. All three feed the same narration prompt.
