# Changelog

## Unreleased

- `start_recording` / `stop_recording`: server-side microphone capture
  for clients that can't record themselves (the VS Code extension). Needs
  the optional `recording` extra (`sounddevice`). The browser UI is
  unchanged.
- STT uploads are labelled `audio/wav` when the audio is WAV.

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
