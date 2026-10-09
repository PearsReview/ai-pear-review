---
name: prep-review
description: Investigate the uncommitted changes in a git repo and write per-hunk briefings into .briefing/ (plus shared change themes into .context/changeset.json) for the AI Pear Review app to use as grounding. Run this BEFORE opening the review UI, or at the end of a session that made the changes. Use when asked to prep a review, pre-brief changes, record why a change was made, warm the briefing cache, or improve the quality of the app's narration.
---

# prep-review

Investigate this repo's uncommitted changes properly — with real file
reading, grepping and git history — and leave the findings where the
review app will pick them up.

## Why this exists

The app narrates a diff hunk by hunk using a small local model (7B on
CPU is a normal setup). That model gets **one hunk's diff and nothing
else** — not the rest of the file, not the other hunks, not the repo, and
it has no tools. When it doesn't know why a change was made, it does not
say so; it produces something fluent and plausible. Two real recorded
examples: a reply confidently explaining *"optimizing database queries"*
for a diff that only added a `farewell()` function, and a 1,959-line
deletion (a file split into modules) explained as *"a redundant reference
to a previous implementation detail"*.

Sometimes it doesn't get the diff at all: a hunk too large for its context
window is answered from your briefing **alone**. So the briefing has to
stand on its own for those.

You have what that model lacks: the whole repo, git history, and the
ability to actually go and look. A briefing you write here is read
straight into the narration prompt, so the difference is between the app
guessing and the app knowing.

The app never runs you. It reads what you leave behind.

## Two modes

- **Author session** — you made (or helped make) these changes in this
  conversation. You already know *why*: record it. That is the most
  valuable thing this skill can capture, because no later investigation
  can recover what the author knew. Use `--source author-session` on
  themes. Still verify `related` links and `summary` against the code.
- **Investigation** — someone else's changes. Do the digging in step 3 and
  use `--source investigated`.

## What to do

### 1. See what needs work

```bash
python .claude/skills/prep-review/scan_hunks.py --todo-only
```

Every hunk the app will review, each with its `diff`, its
`content_hash`, where its briefing belongs, and a `state`:

| state | meaning |
|---|---|
| `missing` | no briefing yet — or only the app's own quick guess from the diff, which yours replaces |
| `stale` | one exists but the code changed under it — the app is ignoring it |
| `fresh` | already briefed for this exact diff; skip it |

Drop `--todo-only` to see everything including `fresh`.

### 2. Write the themes first

A theme is the overall reason a group of hunks exists — "split server.py
into handler modules", "add a fallback for oversized hunks". Many hunks
share one; write it once:

```bash
python .claude/skills/prep-review/write_theme.py \
  --id server-split \
  --title "Split server.py into app/handlers and app/web" \
  --why "server.py was 2660 lines against docs/STYLE.md's ~600-line rule; handlers now self-register (docs/STYLE.md Part 3). A pure move: behaviour unchanged." \
  --source author-session
```

Keep `--why` to two or three sentences (≤400 chars). A change with one
purpose has one theme; don't invent a theme per file.

### 3. Investigate each hunk that needs one

(In an author session, much of this you already know — verify rather than
re-derive.) For each hunk, actually look:

- **Read the whole file**, not just the hunk. What is this function for?
  What calls it?
- **`git log -n 5 -- <file>`** and **`git log -S '<identifier>'`** — has
  this been changed before, and why? Was something reverted?
- **Grep for callers** of anything the hunk adds, renames or changes
  signature on. A caller that wasn't updated is exactly the sort of risk
  worth recording.
- **Where did it go / come from?** For deletions and new files, find the
  matching hunk — that's a `related` entry.
- **Look for the tests** that cover it, or note their absence.

**Restating what the diff obviously shows in `intent` is worse than
writing nothing** — it spends the model's limited context on something it
can already see. (`summary` is the one exception; see below.)

### 4. Write it

```bash
python .claude/skills/prep-review/write_briefing.py \
  --file-path "app/server.py" \
  --header "@@ -408,1965 +294,6 @@" \
  --content-hash "<content_hash from scan_hunks.py>" \
  --intent "Nothing here was deleted: every function moved to a module under app/handlers/ or app/web/." \
  --summary "Removes Session, run_llm, every WebSocket handler and the prompt-context helpers from server.py." \
  --kind move \
  --theme server-split \
  --related '[{"file_path": "app/handlers/narration.py", "header": "@@ -0,0 +1,533 @@", "relation": "moved_to", "note": "present_current_hunk, handle_reply"}]' \
  --confidence high
```

Copy `--content-hash`, `--file-path` and `--header` **verbatim** from the
scan output — for `related` entries too. Don't retype or recompute them:
the app matches the hash against the hunk's diff byte-for-byte, and a
mismatch means the briefing is silently ignored — no error, it simply
never appears.

Re-run the scan afterwards; anything you briefed should now read `fresh`.

## The fields

- **`intent`** (required) — *why* this hunk exists, specifically. The one
  thing the diff cannot show. Folded into every narration.
- **`summary`** — *what* the code change does, ≤2 sentences (≤300 chars).
  Only shown to the model when it can't see the diff (too large, a failed
  call, degraded mode) or when `kind` says the diff misleads — so here,
  describing the change is the point. Say what moved/changed in terms a
  reader can check, not line by line.
- **`kind`** — `behaviour` | `move` | `mechanical` | `docs` | `test`.
  `move` and `mechanical` tell the app the diff alone is misleading (a
  pure move looks like a deletion; a 50-file rename looks like 50 edits),
  so it adds `summary` and `related` to the prompt even when the diff fits.
- **`theme`** — the id of a theme from step 2.
- **`related`** — up to 5 other hunks this one belongs with, each
  `{file_path, header, relation, note}`. `relation` is one of `moved_to`,
  `moved_from`, `caller_of`, `called_by`, `test_for`, `tested_by`,
  `docs_for`, `same_edit`. `note` ≤120 chars. The reviewer sees these as
  clickable links, and the model gets their summaries when it can't see
  this diff. **Only link what you verified** — a wrong link sends the
  reviewer somewhere irrelevant with the app's authority behind it.
- **`alternatives_considered`** — what else could have been done, and why
  this way instead. Only when you actually found evidence (a reverted
  commit, a comment, a related PR, or you were the author), never invented.
- **`risk_notes`** — what could break, what wasn't updated, what's
  untested. Concrete and checkable, not "could have bugs".
- **`confidence`** — **`high` or the briefing is never used.** The app
  folds a briefing into the prompt only when confidence is `high`; a
  `low` one is written, cached, and then ignored — every field above with
  it. So `low` is not a weaker hint — it's a record that you looked and
  found nothing beyond the diff. Use it honestly for that, and don't
  hedge with it.

## Rules

- **Never guess.** An invented intent is worse than none: the model will
  state it as fact and the reviewer has no way to know it was made up.
  If you couldn't find out why, that's `--confidence low` with an honest
  `intent`.
- **Say where you looked** when it helps — "reverted in 9c1f2, brought
  back with the null guard" is worth more than a confident abstraction.
- **Keep it short.** These are injected into a prompt with a hard token
  budget on a small model. The writer enforces the caps.
- **Don't restate the diff in `intent`.** The model already has it.

## When to re-run

Any hunk whose code changed since it was briefed goes `stale` and is
ignored by the app until re-briefed. Worth re-running:

- after making further changes to code you already briefed
- after a rebase or a pull that moves the diff around
- at the end of a working session, in author mode, while the *why* is
  still in the conversation

The app prunes briefings that no longer match any hunk when the diff is
refreshed; briefings for unchanged hunks, and themes, survive.
`scan_hunks.py --todo-only` is the cheap way to check; it does no model
work and only lists what actually needs attention.

## Notes

- Briefings are written under `.briefing/` and themes under `.context/`
  in the repo being reviewed. They are about one in-progress change, so
  add them to that repo's `.gitignore` unless you mean to share them.
- `scan_hunks.py` deliberately imports nothing from the app, so this skill
  works against any target repo. The cost is a copy of the app's
  diff-splitting rules — the app's own `tests/test_prep_review_keys.py`
  pins the two together so drift fails loudly instead of silently
  producing briefings the app won't read.
- The app writes its own fallback briefings (`"source": "generated"`) when
  it finds nothing cached. Yours are marked `"source":
  "prep-review-skill"`, and take precedence simply by existing first.
