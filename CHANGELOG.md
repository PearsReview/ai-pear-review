# Changelog

## Unreleased

- Startup checks: a pull request review counts the PR's commits since its
  merge-base instead of warning that the (normally clean) checkout has nothing
  to review, and `provider: openai` is checked (package, model, key) instead
  of being reported as unknown.
- The reviewed repo's `git status` stays clean in more cases: the debug
  folders (`.briefing_debug/`, `.editor_debug/`) are excluded too, a linked
  worktree (such as a pull request checked out with "Checkout in Worktree")
  gets its exclusions in the main repository's `info/exclude` instead of none,
  and an existing `.briefing_debug` entry no longer counts as `.briefing`.
- An ended review no longer goes quiet: replies, explaining a change on
  request and Look deeper keep working. Reviewed marks, comments and Act Now
  stay locked, and now say so ("The review has ended — reopen it to …")
  instead of silently doing nothing. New `reopen_review` message and
  **Reopen Review** button (on the summary and while browsing after the end)
  pick the same review back up, marks and comments included.
- `provider: openai` talks to any OpenAI-compatible endpoint: a self-hosted
  server, or a gateway (LiteLLM, vLLM, an enterprise LLM proxy) fronting
  several model families. Set `conversation.openai.base_url` and `model`; the
  key comes from `OPENAI_API_KEY` (or the env var `api_key_env` names). The
  settings panel lists the endpoint's models. Streamed and cancellable, like
  the Anthropic provider. Adds the `openai` package to `requirements.txt`;
  without it, only this provider is unavailable.
- `~/.config/pear-review/config.yaml`, if present, is deep-merged over
  `app/config.yaml` at startup, for settings you want across every repo.
  `REVIEW_USER_CONFIG` names a different file.
- `install_skills.py --repos-file FILE` installs into every repo listed in
  FILE; `--check-repos FILE` reports which have missing or outdated skills
  and writes nothing. Installed skills are now added to the target repo's
  `.git/info/exclude`, so they no longer show up as changes in the review.
- `pip install -e .` provides `pear-review` and `pear-install` commands.
- STT: a lone "you" is no longer discarded as a Whisper hallucination.

## 0.0.2

- The VS Code extension now lives in this repo, in `vscode/` (its history
  came with it); there is one repo and one version for both front ends. The
  web app is unchanged. See [vscode/CHANGELOG.md](vscode/CHANGELOG.md).
- Security: `.review/ui_settings.json` is input from the repo under review.
  A copy committed to the repo is ignored, and any copy is held to the settings
  panel's allowlists on load. Before, a repo could ship one that replaced the
  coding agent's command (run by Act Now / Look deeper) or pointed the model
  and speech services at another server.
- `start_recording` / `stop_recording`: server-side microphone capture
  for clients that can't record themselves (the VS Code extension). Needs
  the optional `recording` extra (`sounddevice`). The browser UI is
  unchanged.
- STT uploads are labelled `audio/wav` when the audio is WAV.
- `review_progress` file entries also list their hunks (`index`, `header`,
  `reviewed`), for clients that show hunks individually.
- `explore_reply` accepts optional `marked_lines`, the VS Code extension's
  editor selection, as context for a question about a file.

## 0.0.1 — beta

First public release. Everything below is new.

- Review uncommitted changes against `HEAD` one hunk at a time, in a
  full-file diff (Merged or Split) with the current hunk highlighted.
- **Explain**: an AI persona narrates a hunk on demand (or automatically),
  and answers follow-up questions typed or spoken.
- Narration through a local Ollama model (default) or the Anthropic API.
- Line marking for question context, and GitHub-style inline comments that
  **Create plan** turns into a hand-off document and an optional
  `/apply-review` skill.
- **Act Now** (apply a small agreed change after a preview) and **Look
  deeper** (read-only investigation), both through Cline over the Agent
  Client Protocol, in a temporary copy of the repo.
- **All files** mode for asking about any file in the repo.
- Review progress and queued comments survive a refresh or restart.
- Optional voice in and out through a configurable STT/TTS endpoint.
- Prep skills (`project-overview`, `call-map`, `prep-review`) for Claude
  Code or Cline, installed with `install_skills.py`.
- A startup check that reports what is missing and how to fix it.

Known limitations are listed in the README under
[Known issues and limitations](README.md#known-issues-and-limitations).
