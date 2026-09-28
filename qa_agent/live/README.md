# qa_agent/live — real-repo regression + accuracy suite

Drives the AI Pear Review app against a **real external repo's
real uncommitted changes**, rather than the fixed scratch repo
(`qa_agent/`) or a seeded synthetic one (`qa_agent/generated/`). Both of
those exist to be deterministic and easy to test against; this suite
exists for the opposite reason — to prove the app holds up on a diff that
wasn't built to be easy.

## Running

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium

pytest qa_agent/live/ --target-repo path/to/any/repo
```

`--target-repo` is required and accepts any git repo with something actually
uncommitted (`git diff` vs `HEAD`, or an untracked file) — nothing in this
suite is hardcoded to one repo's file names or hunk count; `total_hunks` is
computed fresh each run from the target's own `git diff`. A repo with
nothing uncommitted fails fast with a clear message rather than silently
testing nothing.

Excluded from `pytest qa_agent/` (see `qa_agent/conftest.py`'s
`collect_ignore`), same as `qa_agent/generated/` — it needs its own
explicit invocation, takes real minutes against a real local model, and
depends on an external repo path existing on this machine.

**Session cleanup:** this suite treats `target_repo` as a real project it
doesn't own. `.review/` and `.briefing/` — the app's own artifacts, not
the target project's — are removed from it at both session start and end
(`conftest.py`'s `target_repo` fixture). Worth knowing outside this suite
too: a target repo's `.gitignore` has no reason to know about either
directory, so leaving them in place makes them show up as untracked files
on the *next* run, and the app treats every untracked file as a
reviewable "hunk" — confirmed live once already (see git history).

## What's covered

- **`test_live_review.py`** — starts the review, walks every real hunk via
  Next, and for each one: asserts non-empty narration, records a
  semantic-judge verdict (`qa_agent/judge_prompts.py`'s
  `NARRATION_JUDGE_*`, via the app's own configured Ollama model — see
  "Judging" below), and asserts no WebSocket `error` frame was ever sent.
  Writes two result packs (see below).
- **`test_live_explore_mode.py`** — the "All files" toggle against
  target_repo's real, pre-existing folder tree (not a seeded one),
  opening a real unchanged file and asking a real question about it.
- **`test_live_review_actions.py`** — marks a real hunk reviewed, queues a
  real inline comment, and confirms Finish Review writes the hand-off
  document to disk inside `target_repo`, not just over the wire.

## Judging: two independent opinions, not one

Every hunk's narration gets **two separate verdicts**, deliberately not
merged into one:

1. **The suite's own judge** (`judge_and_record`, recorded live during the
   walk) — the same small local Ollama model (`app/config.yaml`'s
   `conversation.ollama.model`) the app itself uses to narrate, voting 3x
   (`judge_with_voting`). Cheap and automatic, but a small model judging
   its own kind of output has real, documented limits — see
   `qa_agent/test_semantic_quality.py`'s module docstring. Recorded to the
   shared `qa_agent/findings.jsonl`, same as the sibling suites, and to
   each pack entry's `local_verdict`.
2. **A Claude-authored verdict**, given afterward by an actual capable
   model with real repo access — via the
   [`judge-live-review`](../../.claude/skills/judge-live-review/SKILL.md)
   skill, run by hand in your own interactive Claude Code session (never
   automatically — see that skill's own reasoning for why). Its whole
   value is disagreeing with #1 when #1 is wrong; see that file's own
   `_write_markdown` for how a disagreement gets surfaced first.

Neither this suite nor that skill hard-asserts on verdict *content* —
only that a verdict was recorded at all
(`test_judge_recorded_a_verdict_for_every_hunk`). Same "record, don't
gate on a small model's opinion" convention the rest of `qa_agent/`
follows.

## Result packs

`test_review_pack_is_written` writes two files per target repo under
`qa_agent/live/results/`:

- `<repo>_live_review_pack.md` — human-readable: every hunk's diff,
  narration, and local judge verdict, in one place.
- `<repo>_live_review_pack.json` — the same data, machine-readable, for
  the `judge-live-review` skill's scripts to read. Shaped generically
  (`category`/`question`/`answer_text` rather than
  `narration`-specific fields) so a future reply/grounding category
  (Q&A about changed code, Q&A about unchanged code — see the plan this
  suite was scoped from) slots in without a reshape.

After a run, judging it further is a separate, deliberate step — see
"Judging" above:

```bash
python .claude/skills/judge-live-review/scan_pack.py --todo-only
# ...then actually read each item and write a verdict, per that skill's SKILL.md
```

## Not yet built

- **Q&A accuracy on changed code** — the walk narrates every hunk but
  never asks a follow-up question and judges the reply (the sibling
  suite's `REPLY_JUDGE_*` rubric, applied to a real hunk).
- **Q&A accuracy on unchanged code** — `test_live_explore_mode.py` asks
  one generic question per test with no judge attached; a grounding
  rubric (`EXPLORE_REPLY_JUDGE_*`-equivalent) would give it the same
  verdict treatment narration already gets.
- **Whole-repo explanation** — `project_overview.json`'s digest gets
  injected into every prompt once written, but writing one is a
  judgement call made by a model reading the whole repo in an
  interactive session (the `project-overview` skill), not something this
  suite can script the way `call-map` is. Needs that skill run against
  `target_repo` once, by hand, before any test here could exercise it.
- **Dependency/logic questions** — `call-map` (`.claude/skills/call-map/`)
  is a pure `ast` scanner with no model involved, so — unlike whole-repo
  explanation — this one *is* fully scriptable, and it's the one place
  this suite could get a real oracle (assert a claimed caller actually
  appears in the scanned call graph) instead of another LLM judge's
  opinion.
