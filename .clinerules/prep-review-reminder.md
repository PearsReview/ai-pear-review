# Briefing the changes you make

This repo is reviewed by the AI Pear Review app, which narrates each diff
hunk with a small local model that sees **one hunk and nothing else** — no
other files, no history, no tools. When it doesn't know why a change was
made it doesn't say so; it invents something plausible. The `prep-review`
skill writes per-hunk briefings into `.briefing/` that the app reads
straight into that narration prompt.

The reason for a change is only fully known in the session that made it.
Once this conversation ends, no later investigation recovers it.

## Before you end a task that changed files

Check whether the changes you just made are briefed (a briefing the app
wrote itself, from the diff alone, doesn't count — the scan reports those
as `missing`):

```bash
python .claude/skills/prep-review/scan_hunks.py --todo-only
```

- **It reports hunks** (`missing` or `stale`) **in files you edited in this
  task** — brief them now, before your closing message, without asking:
  follow `.claude/skills/prep-review/SKILL.md` in author-session mode for
  those hunks only, recording why you made each change. Skip hunks you
  didn't write, even in a file you edited: someone else's change isn't
  yours to explain, and a guessed reason is worse than none. If there are
  more than 15, brief the 15 you edited last and say how many are left.
- **It reports hunks only in files you didn't edit** — leave them. Don't
  mention them.
- **It reports nothing, or the repo has no uncommitted changes** — say
  nothing. Don't mention the check.
- **It fails** (not a git repo, no `git`, script missing) — say nothing and
  carry on. This must never get in the way of the work.

Don't run this on a task that only read code. If the user has said not to
brief changes in this session, don't.
