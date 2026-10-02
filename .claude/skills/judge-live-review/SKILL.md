---
name: judge-live-review
description: Read qa_agent/live/'s results (after `pytest qa_agent/live/` has run against a real target repo) and give each narration/reply a real accuracy verdict using this actual agent session — not the small local Ollama model that suite's own judge uses. Run this AFTER the live suite, in your own interactive session (Claude Code or Cline). Use when asked to judge the live review results, validate live-suite answers, check narration/reply accuracy with Claude, or double-check what the qa_agent live judge found.
---

# judge-live-review

Give a second, real opinion on what `qa_agent/live/` recorded — using this
session's own reading of the diff (and, when useful, the actual file).

## Why this exists

`qa_agent/live/` already judges every narration it records (see
`qa_agent/judge_prompts.py`'s `NARRATION_JUDGE_*` and
`qa_agent/llm_client.py`) — but that judge is the **same small local
Ollama model** the app itself uses to narrate, voting on its own kind of
output with no ability to open a file, run a search, or check git
history. `judge_with_voting`'s k=3 self-consistency voting exists
specifically because a single verdict from that setup isn't reliable.

You can actually read `target_repo`, follow an import, check whether a
claimed caller exists, or notice that a narration invented something the
diff never touched — a qualitatively different check than another vote
from the local judge, which is why this is a separate skill rather than
"run the local judge again."

**This never runs automatically.** The live suite calls only its own
local judge; nothing in `qa_agent/` or the app itself ever shells out to
`claude`. You run this yourself, in your own interactive session, after
`pytest qa_agent/live/` has produced something to judge — spending your
own subscription's usage, the same deliberate boundary the other three
skills in `.claude/skills/` already draw (see their own SKILL.md files,
and `docs/architecture.md`'s note on why the app never automates `claude` CLI
auth itself).

## What to do

### 1. See what needs judging

```bash
python .claude/skills/judge-live-review/scan_pack.py --todo-only
```

One entry per item (currently: one per hunk narration; more categories —
replies to questions about changed code, replies about unchanged code —
land here the same way once qa_agent/live/ grows them, with no change to
this skill needed) with:

| field | meaning |
|---|---|
| `category` | what kind of item this is (`narration` today) |
| `file_path`, `diff` | what was shown to the app |
| `question` | null for narration; the reviewer's question for a reply-type item |
| `answer_text` | what the app actually said |
| `local_verdict` | the live suite's own Ollama judge's verdict/reason/votes |
| `content_hash` | this item's identity — copy verbatim into write_verdict.py |
| `state` | `missing` / `stale` / `fresh` — see scan_pack.py's own docstring |
| `target_repo` | absolute path to the repo under review, if you want to open the real file for more context than the diff alone gives |

Drop `--todo-only` to see everything including already-`fresh` items. Pass
`--pack path/to/X_live_review_pack.json` to scope to one target repo if
more than one pack exists under `qa_agent/live/results/`.

### 2. Actually judge each item

For a `narration` item: does it accurately describe what the diff
changed, without inventing anything the diff doesn't show? Does it read
as a natural description rather than a mechanical line-by-line readout?
Is it a coherent, plausible thing to say about this specific change?
(Same three questions `NARRATION_JUDGE_SYSTEM` asks the local judge —
the difference is you can actually check them instead of pattern-matching
on them.)

Worth doing that the local judge structurally can't:

- **Open `file_path` under `target_repo`** if the diff alone leaves a
  claim unverifiable — is the surrounding code what the narration implies
  it is?
- **Check specifics.** A narration claiming "improves performance" or
  "adds error handling" — does the diff actually show that, or is it a
  plausible-sounding paraphrase with no basis in the visible lines?
- **Notice invention.** A narration can describe what a diff like this
  *usually* does rather than what *this one* does. You're reading the
  actual lines; use that.
- **Don't rubber-stamp `local_verdict`.** A "yes" there doesn't make
  something true. Read the diff and answer yourself before looking at
  what it said, if you want to avoid being anchored by it.

For a `reply`-type item (once qa_agent/live/ has one): also weigh whether
the answer is relevant to the question asked, not only accurate about the
diff — a factually-correct answer to a different question than what was
asked should not be "yes".

### 3. Write it

```bash
python .claude/skills/judge-live-review/write_verdict.py \
  --pack qa_agent/live/results/<target>_live_review_pack.json \
  --content-hash <content_hash from scan_pack.py> \
  --verdict yes \
  --reason "Accurately describes the retry change; the timeout claim is directly supported by the comment added in the same hunk."
```

Copy `--pack` and `--content-hash` **verbatim** from scan_pack.py's
output — don't retype or recompute either; a mismatched hash writes a
verdict for content that no longer exists in the pack.

This writes/updates two files next to the pack:

- `<repo>_claude_review.json` — one record per judged item, keyed by
  `content_hash`. What `scan_pack.py` reads back next time to compute
  missing/stale/fresh.
- `<repo>_claude_review.md` — regenerated on every write. **Disagreements
  with the local judge are listed first** — that's the one signal this
  whole skill exists to surface; everything else is confirmation.

Re-run the scan afterward; anything you judged should now read `fresh`.

## The verdict values

Same three values the local judge uses, so a comparison is meaningful:

- **`yes`** — accurate and grounded; you checked and it holds up.
- **`no`** — inaccurate, invented, or answers something other than what
  was asked. Say specifically what's wrong in `--reason` — "vague" is not
  actionable, "claims this adds retry logic; the diff only adds a log
  line" is.
- **`unsure`** — genuinely ambiguous (rare) or you don't have enough
  context to verify a specific claim even after checking `target_repo`.
  Not a hedge for "didn't look closely."

## Rules

- **Read the actual diff, not just the local judge's reason.** The point
  is an independent check; anchoring on the existing verdict defeats it.
  An unverified "yes" here is worse than the local judge's own vote — a
  Claude-authored rubber stamp reads as authoritative.
- **Say what's actually wrong, concretely**, same as prep-review's
  `intent` field — a reason a person can act on, not a restatement of
  "yes" or "no."
- **Only write via the script.** Hand-editing `<repo>_claude_review.json`
  risks a shape `scan_pack.py`/`write_verdict.py` won't recognize on the
  next run, same reasoning as prep-review's briefing files.

## When to re-run

Any item whose `qa_agent/live/results/*_live_review_pack.json` entry
changes — a re-run of `pytest qa_agent/live/` against a moved diff, or
against a different `--target-repo` — goes `stale` (or is simply new) and
needs judging again. `scan_pack.py --todo-only` is the cheap way to check;
it does no model work and only lists what actually needs attention.
