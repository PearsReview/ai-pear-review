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

Check whether the changes you just made are briefed:

```bash
python .claude/skills/prep-review/scan_hunks.py --todo-only
```

- **It reports hunks** (`missing` or `stale`) — say so in your closing
  message, with the count, and offer to run `prep-review` in author-session
  mode now. If the user says yes, follow
  `.claude/skills/prep-review/SKILL.md`.
- **It reports nothing, or the repo has no uncommitted changes** — say
  nothing. Don't mention the check.
- **It fails** (not a git repo, no `git`, script missing) — say nothing and
  carry on. This is a reminder; it must never get in the way of the work.

Don't run this on a task that only read code, and don't repeat the offer in
a session where the user already declined it.
