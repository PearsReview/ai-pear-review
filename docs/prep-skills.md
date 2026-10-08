# Prep Skills

Optional context the app reads but never generates itself. Run these in
your own coding agent before opening the review UI.

Companion docs: [README.md](../README.md) (how to run it),
[architecture.md](architecture.md) (what the pieces are).

---

The model narrating your diff is often small and local, and whatever it
is, it sees **one hunk and nothing else** — no repo, no tools, no idea what the project is. When it
doesn't know something it doesn't say so; it produces something fluent and
plausible.

Three skills close that gap. You run them yourself, in your own coding
agent, *before* opening the app; the app only ever *reads* what they leave
behind and never invokes an agent itself. All three are optional — with
none of them run, `python run.py` behaves exactly as it always has.

| Skill | Answers | Writes | Rerun when |
|---|---|---|---|
| **project-overview** | What is this project, what is this file for, what are the house rules? | `.context/project_overview.{json,md}` | the architecture moves |
| **call-map** | Who calls the code in this hunk? | `.context/call_map.{json,md}` | HEAD moves (it's a one-second scan) |
| **prep-review** | Why was *this hunk* written this way? | `.briefing/*.json` | the diff changes |

### Installing them into the repo you're reviewing

The skills have to live in the repo being reviewed: that's the repo your
agent session is open on, and the one their scripts scan by default. Copy
them there once per repo:

```
python /path/to/this/app/install_skills.py --repo .
```

They land in `.claude/skills/`, which **both supported agents discover on
their own** — Claude Code reads it, and so does Cline (which also accepts
`.cline/skills/`, so one copy serves both). Open your agent in that repo
and the three skills are available by name.

Rerun the installer after updating this app: files that already match are
left alone, and a skill you've edited yourself is reported and kept unless
you pass `--force`. `--dry-run` says what would change. It refuses a
directory that isn't a git repository, since the skills describe
uncommitted changes.

If you review several repos, two flags save going one at a time:
`--repos-file repos.txt` installs into every path listed in a file (one per
line; blank lines and `#` comments ignored), and `--check-repos repos.txt`
reports which of them have missing or out-of-date skills — handy after
updating this app — without writing anything.

The copies are ordinary files in that repo. The installer adds the three
skill directories to the repo's `.git/info/exclude`, so a fresh install
doesn't show up as untracked changes in your first review. That exclude is
local and uncommitted, so committing the skills (shared prep for your team)
or gitignoring them (`/.claude/skills/`) for everyone is still your call.
Whether to commit what they *write* is a separate decision: `.context/` is
repo-level and worth sharing, while `.briefing/` and
`.review/` are about one in-progress change (the app keeps those three out
of the review itself).

### Reminders (optional)

```
python /path/to/this/app/install_skills.py --repo . --with-reminders
```

adds a nudge to run prep-review before a session that changed code ends,
while the agent still knows *why* each change was made:

- **Claude Code:** a Stop hook, `.claude/hooks/briefing_reminder.py`,
  registered in `.claude/settings.local.json`. That's your personal
  settings file rather than the shared `settings.json`, so it doesn't turn
  the hook on for everyone else working in the repo. Existing settings are
  kept, and a settings file that isn't valid JSON is reported and left
  alone. The hook only shows a message, never blocks, and stays silent if
  anything goes wrong.
- **Cline:** a rule, `.clinerules/prep-review-reminder.md`, asking the agent
  to check for unbriefed hunks before finishing and offer to run
  prep-review. If your `.clinerules` is a single file rather than a folder,
  the rule is skipped, since adding it would mean editing your file.

It's opt-in because it changes how your agent behaves in that repo, not
just which skills it can be asked to run.

### Running them without installing

The scripts all take `--repo`, so for a one-off review of a repo you'd
rather not add files to, run them from this app's folder instead:

```
python .claude/skills/call-map/scan_calls.py --repo ~/your-project
python .claude/skills/prep-review/scan_hunks.py --repo ~/your-project --todo-only
```

The skills themselves are instructions for an agent, so this route suits
an agent session opened in *this* app's folder: ask it to run the skill
against `~/your-project`. It follows the same SKILL.md and passes `--repo`
to each script.

Each skill's `--status` tells you whether it's worth rerunning. The
overview is the expensive one, so it has a cheap path for the common case:

```bash
python .claude/skills/project-overview/write_overview.py --status
# ...reports the commits since, and which files were added/deleted/renamed
python .claude/skills/project-overview/write_overview.py --restamp
```

`--status` filters to files **added, deleted or renamed** — a project's
shape moves when files appear, vanish or move, not when someone edits one.
If nothing structural happened, `--restamp` marks the existing overview as
still-accurate at the new HEAD without rereading anything. That matters
because the honest common answer is "still right, just old", and if that
answer costs a full re-read, nobody pays it and the overview rots.

The first two are repo-level and land in the narration prompt like this:

```
About this project: A payments service for a bicycle rental company...
About billing.py: Charging riders; the only place money is taken.

Who calls the code in billing.py (from a recorded call map —
this is not visible in the diff):
- charge is called by: checkout (flow.py), refund (refunds.py)
```

That last line is the one you can't get any other way, and it's what makes
"what else depends on this?" an answerable question in the chat.

One more block needs no skill at all — the app derives it from the diff it
already has, so it's there even on a repo where you've run nothing:

```
This is hunk 1 of 3 in the change being reviewed, which spans 3 files.
Besides billing.py, it also touches: flow.py, refunds.py. You cannot see
those files, so do not guess what they contain or how they relate.
```

Without it the model treats every hunk as the whole change, and explains
one leg of a nine-file rename as though it were a standalone edit.

Two deliberate differences between them are worth knowing:

- **project-overview is written by the model; call-map is a script.** An
  overview is a judgement and needs a reader who understands the code. A
  call map is mechanical — either `checkout()` calls `charge()` or it
  doesn't — and asking a model for mechanical facts invites confident
  invention. A fabricated caller is worse than a missing one, because the
  app states it to you as background fact.
- **Neither ever claims an absence.** The call map is name-based and
  Python-only, so it under-reports by design; the app never renders
  "nothing calls this", because a scan finding nothing is not the same as
  nothing existing.

The app's **settings panel** (gear icon) shows how old each `.context/`
file is and which skill regenerates it. It's shown there rather than as a
banner because stale prep degrades nothing — narration just runs without
it.

If you want to write your own equivalent of the briefing skill, the
contract it needs to honor is in `app/services/briefing_service.py`:

- Cache file path: `{repo_path}/.briefing/{file_path with / and \\
  replaced by __}__{first 8 hex chars of sha256(hunk header)}.json`
- Cached content is validated against a hash of the hunk's *exact* diff
  text, not just its position — a stale cache entry (code changed since
  it was written) is detected and regenerated automatically.
- JSON shape: `{"intent": str, "alternatives_considered": str | null,
  "risk_notes": str | null, "confidence": "high" | "low"}`.

Two `tests/` files pin the Skill↔app agreement, because drift here is
silent and total — a briefing written under a key the app doesn't look up,
or a call map in a shape it doesn't recognise, is simply never read, with
no error and no warning: `tests/test_prep_review_keys.py` and
`tests/test_call_map_skill.py`.
