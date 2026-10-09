# To do

## Backend-side refactors

- [ ] **Launching for the extension.** `python/launch.py` imports `run.py`'s
      internals (`parse_args`, `find_free_port`, `REPO_PATH_ENV`), the preflight
      and `CONFIG`, so a backend refactor can break it with the backend's tests
      still green. Give `run.py` a supported mode that prints
      `PEAR_REVIEW_PORT=<port>` (e.g. `--announce-port`), with a backend test;
      the extension runs `backend/run.py` and `launch.py` goes.
- [ ] **Shared chat scripts.** `media/chat/blocks.js` and `media/chat/readalong.js`
      are ports of `static/js/md-preview.js` and `static/js/audio.js` ("keep the
      two in step"); the weighting maths is in a third place,
      `src/review/reading.ts`. Pull the pure parts into dependency-free modules
      the web app imports and the extension copies at build (as it does
      codicons), then delete the copies.
- [ ] **Test config without text patching.** `test/integration/fixtures.ts`
      rewrites `config.yaml` by string replacement. A config override the
      backend supports (a path or environment variable) would survive the
      file's formatting.

## Before sharing

- [x] `repository`, `bugs` and `homepage` in package.json (the remote is
      https://github.com/PearsReview/ai-pear-review), then drop
      `--allow-missing-repository` / `--no-rewrite-relative-links`.
- [x] Remove `"private": true` from package.json (`vsce publish` refuses it).
- [x] A 128×128 PNG icon (`icon` in package.json).
- [ ] The `PearsReview` publisher created on the Marketplace, with a PAT for
      `vsce login` (an account step, not code).
- [x] The backend's mypy errors: `mypy` is clean and CI's Types job passes.
- [x] **Refresh the Python environment on upgrade.** Each backend start
      compares a stamp of `requirements.txt` kept in the managed venv
      (`backend/pythonPackages.ts`) and reinstalls when a release changed it,
      so a package a release adds (as `openai` was) no longer stays missing.

## Not yet verified

- [ ] **Set Up Python Environment** end to end, from the `.vsix` in a clean
      VS Code profile.
- [ ] The manual F5 walkthrough (README, "Developing"), and
      `npm run test:integration:ollama`.
- [ ] Multi-root test: the first repository showed a sixth changed file after
      switching away and back. Find out what it is.

## Code

- [x] `chatPanel.register` takes 8 positional parameters, `voice.register` 5:
      pass a deps object.
- [ ] Keep the chat transcript across window reloads (webview `getState` /
      `setState`); it lives only while the view is retained.
- [x] Type `context_too_large` in `protocol.ts` (the chat reads it; it's still
      `Unknown`).
- [x] Run ruff on `python/` and `test/python/` in CI, with the backend's settings.
- [ ] **Callers and tests from the coding agent.** The call map is switched
      off: narration and replies no longer read `.context/call_map.json`, and
      it's left out of Review context and the out-of-date notice. It was Python
      only. When a question asks who calls the code or whether it's tested
      (the old keyword routes, `_CALLERS_RE` / `_TESTS_RE`, in git history of
      `app/web/question_context.py`) and an agent is set, run Look deeper with
      a focused, read-only prompt: list every caller and test as `file:line`.
      Check each cited line names the symbol before showing it. With no agent,
      say nothing. Then delete `.claude/skills/call-map`,
      `app/services/call_map.py`, their tests, the skipped live dependency
      checks, and the call map's entries in `install_skills.py` and the docs.
- [ ] **The PR's own text as the "why" in PR reviews.** A PR review asks for no
      briefings (`prep_status` counts none as out of date when
      `REVIEW_BASE_SHA` is set), so its hunks are explained from the diff
      alone. `pullRequests.ts` already calls `github.getPull()`, which returns
      the body; keep it beside the title and pass both to the backend (with
      `protocol.ts`, `backend.ts`, `docs/wire-protocol.md`). The backend gets
      commit subjects itself with `git log --format=%s <base>..HEAD`. Put them
      in the narration prompt in words as the author's claims ("The author
      says: …"): a PR body is untrusted text. Cap it at about 1–2k characters
      and drop empty or template-only bodies.
- [ ] **Brief PR changes from the Jira ticket, the plan and CLAUDE.md.** Run
      the prep-review skill on a PR too, in investigation mode, with the
      ticket, the `plan.md` the author worked from and the repo's `CLAUDE.md`
      as evidence. Not straight into every explanation: they're long and
      drift from the code, and explanations are kept to about 60 words. Needs
      `scan_hunks.py --base <sha>`, so the scanner sees exactly the hunks a
      PR review shows (`get_review_hunks` with `REVIEW_BASE_SHA`); the
      briefing keys and hashes then work as they do today.
- [ ] **What the skill does with that context.** Read it from a folder such as
      `.review/pr_context/` (`jira.md`, `plan.md`), plus `CLAUDE.md` and the
      PR's title, body and commit subjects. Write the theme from the ticket,
      explain hunks from the plan, check each claim against the code, record
      mismatches as `risk_notes` ("plan says retry 3 times; code retries
      once"), and say which source a reason came from: this is
      investigation, not the author's own account. In a PR review, bring
      back the "not briefed" warning and its Brief in Claude Code / Cline
      buttons (off now when `REVIEW_BASE_SHA` is set), with a way to add the
      ticket and plan.
- [ ] **Open questions on PR reviews.** How the ticket is fetched (Claude
      Code's Atlassian connection from a key, or pasted/exported text);
      where `plan.md` lives (committed in the PR branch, or a local file from
      the author's session); what `CLAUDE.md` should add beyond the project
      overview; and anything else unsettled about PR reviews.

## Docs and comments

- [x] README step 2 says Start Review makes the reviewer "explain each change as
      you go"; explaining is on request by default now (step 4).
- [x] README "Tests": says three layers, misses `test/python` and
      `test:package`, and the `:ollama` run covers only the main workspace.
- [x] README step 4 has an unwrapped line; step 10 and the paragraph after
      step 12 pack several topics each, and say more than a reader needs.
- [x] `src/backend/backend.ts`: "Errors are shown in the chat panel too":
      they're notifications now (`notices.ts`).
- [x] `src/ui/chatPanel.ts`: "the rest arrive as their UI is built": the
      other messages are shown elsewhere by design.
- [x] `src/backend/python.ts`: the module comment lists the lookup order the
      function already spells out; keep only why `python` on PATH isn't used.
- [x] `docs/STYLE.md` §3: "as `files.ts` was into …" is changelog; cut it.
