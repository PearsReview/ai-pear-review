---
name: project-overview
description: Study a codebase and write .context/project_overview.{json,md} — what the project is, its main pieces, entry points and conventions — so the AI Pear Review app can ground its narration in what the project actually is. Run before reviewing, and again when the architecture changes. Use when asked to generate a project overview, document what a codebase does, or give the review app repo-level context.
---

# project-overview

Work out what this project actually is, and write it where the review app
will read it.

## Why this exists

The review app narrates a diff with a small local model that is handed
**one hunk's diff and nothing else**. It has no idea what the project is,
what the file it's looking at is for, or how the pieces fit. So it fills
the gap with something plausible — which is how you get a two-line change
explained in terms of a subsystem the repo doesn't have.

You can read the whole repo. Two or three paragraphs of accurate
orientation, written once, improve every narration the app produces after
it.

The app never runs you. It reads what you leave behind, and works fine
without it — so a missing overview costs nothing, but a **wrong** one is
worse than none, because the model will state it as fact.

## What to do

### 1. Check what's already there — this decides everything below

```bash
python .claude/skills/project-overview/write_overview.py --status
```

**Start here, always.** Most of the time the answer is "still accurate,
just old", and that answer should cost a second, not a full re-read.
`--status` gives you the age, the commit count, the commits themselves,
and — the useful part — the files **added, deleted or renamed** since.
Those are filtered deliberately: a project's shape moves when files
appear, vanish or move, not when someone edits the body of an existing
one.

Three outcomes:

| `--status` says | Do this |
|---|---|
| No overview exists | Full study — step 2 |
| HEAD moved, **no** files added/deleted/renamed | Skim the commit list. If nothing changed what the project *is*, `--restamp` and stop |
| HEAD moved, files **were** added/deleted/renamed | Read the existing overview, read those files, **edit the parts that are now wrong** — step 3 |

```bash
# "still accurate, just old" — moves the stamp, changes nothing else
python .claude/skills/project-overview/write_overview.py --restamp
```

Only start from scratch when there's no overview, or the project is
genuinely unrecognisable. Editing an existing overview is both cheaper and
usually *better* — the existing wording was written with the whole repo in
view, and rewriting from scratch tends to lose accurate detail for no
reason.

### 2. Study the repo (first time, or a genuine rewrite)

Use what's already written down before inferring anything:

- **README, docs, architecture notes** — start here. If the project
  explains itself, your job is to distil, not to re-derive.
- **Entry points** — what does someone actually run? `main`, `run.py`,
  `package.json` scripts, a CLI, a server.
- **Directory layout** — what is each top-level area *for*? Follow an
  import or two to check the name matches the reality.
- **Config** — what's configurable usually reveals what the project cares
  about.
- **Tests** — often the clearest statement of intended behaviour.

Prefer what the code does over what the docs claim. Where they disagree,
that disagreement is itself worth a line.

### 3. Write it

When **updating**, start from the existing `.context/project_overview.json`
— read it, change what's now wrong, keep what's still right — rather than
composing a fresh one. When writing the first time, prepare a JSON file:

```json
{
  "digest": "A local web app that walks a reviewer through their uncommitted git changes one hunk at a time, narrating each with a local LLM and supporting inline review comments and small applied edits. FastAPI + WebSocket backend, plain HTML/JS frontend, no build step.",
  "components": [
    {"path": "app/server.py", "role": "FastAPI app and the whole WebSocket protocol; every client action is a message handler here."},
    {"path": "app/services", "role": "Service layer: git diff reading, LLM clients, briefing cache, voice adapters."},
    {"path": "static", "role": "The browser UI — plain HTML/CSS/JS served as-is, no build step."},
    {"path": "qa_agent", "role": "Playwright suite that drives the real app and LLM-judges its output."}
  ],
  "entry_points": ["`python run.py` — starts the server and opens the review UI"],
  "conventions": [
    "Derived caches live in dotted dirs at the repo root (.briefing/, .review/, .context/) and are gitignored.",
    "The app never invokes the Claude CLI itself; anything needing that is handed to the user to run."
  ]
}
```

Then:

```bash
python .claude/skills/project-overview/write_overview.py --from overview.json
```

That writes both `.context/project_overview.json` (what the app reads)
and `.context/project_overview.md` (what a human reads), stamped with the
current HEAD.

## What the app actually injects

This matters for how you write it. The app does **not** inject the whole
file. Per narration it injects:

- the **`digest`**, always
- the **one `components` entry** whose path matches the file being
  reviewed — exact match wins, otherwise the longest matching directory
  prefix

So:

- **`digest`** is the single most important field. It must stand alone
  and be worth the budget: what the project is, what shape it has. A few
  sentences.
- **`components[].role`** should read as a useful sentence about *that
  file or directory* on its own, since it will appear without any of its
  neighbours for context.
- **List areas, not every file.** A directory entry covers everything
  beneath it, so `app/services` earns its place while forty individual
  service files do not.
- **`conventions`** — the first few reach the model, tightly capped. Write
  them as rules a reviewer could check a diff against ("derived caches go
  in gitignored dot-dirs at the repo root"), not as trivia. Put the most
  load-bearing one first; the rest are for the `.md`.
- **`entry_points`** is for the human-facing `.md` only.

## Length is a hard constraint

The model runs on a few thousand tokens total, and everything you add
comes out of the budget the **diff** needs. The writer enforces limits
(digest ≤ 900 chars, each role ≤ 350, ≤ 40 components) and refuses to
write past them rather than letting the app silently truncate mid-
sentence.

Aim well under those limits. Orientation, not documentation.

## Rules

- **Accuracy over completeness.** A short, certainly-true overview beats
  a thorough one with invented parts. The model cannot tell the
  difference and will assert whatever you write.
- **Describe what is, not what should be.** No recommendations, no
  critique — this is grounding for someone reviewing a diff.
- **Don't describe the diff or current work.** The overview is about the
  project's stable shape; per-hunk detail is the prep-review skill's job.
- **Regenerate when the architecture moves**, not on every commit.

## Notes

- Output goes to `.context/` — deliberately not `.briefing/`, which the
  app prunes as the diff moves on Refresh Diff (`prune_briefing_cache`). An
  overview costs real work and shouldn't die with a re-diff.
- Missing, empty-digest, or malformed overviews are ignored by the app
  and narration carries on exactly as before — see
  `tests/test_project_overview.py`.
- Three skills feed the same narration prompt: this one explains what the
  project *is*, **call-map** explains what *depends on* the code in a hunk,
  and **prep-review** explains why each hunk was written. They're
  independent — run any subset.
