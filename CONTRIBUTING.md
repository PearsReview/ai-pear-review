# Contributing

For what the app does and how to run it, start with
[README.md](README.md). This page is for working on the code.

- [`docs/STYLE.md`](docs/STYLE.md) — house style, and the two patterns the
  code grows along: a new model provider, and a new piece of functionality.
- [`docs/architecture.md`](docs/architecture.md) — the runtime components,
  degraded mode, session persistence, and what happens from `python run.py`
  through a reviewer's first reply.
- [`docs/wire-protocol.md`](docs/wire-protocol.md) — every browser/server
  message, its payload, and the handler that owns it.

## Testing

Two separate suites:

- **`tests/`** — plain `pytest` unit tests that `import app.*` directly
  (e.g. the prompt text built from a reviewer's marked-line selection).
  ```
  pytest tests/
  ```
  This is the suite CI runs, on Linux and Windows, with Python 3.10 and
  3.13 — the floor `pyproject.toml` declares and the version the app is
  developed against.
  See [.github/workflows/ci.yml](.github/workflows/ci.yml).
- **`qa_agent/`** — a Playwright-driven UI regression suite that never
  imports `app.*`; it drives a real `python run.py` subprocess (against a
  disposable scratch git repo, not your real working tree) the way a
  real browser user would. See [qa_agent/README.md](qa_agent/README.md) for
  full details, including a semantic-judging stage that scores
  narration/replies/Act Now proposals via a separate LLM session.
  ```
  pip install -r requirements-dev.txt
  python -m playwright install chromium
  pytest qa_agent/
  ```

## Project layout

```
run.py                     entry point — python run.py
install_skills.py          copies the prep skills into a repo you review
app/
  server.py                FastAPI app, WebSocket connect + dispatch
  handlers/                 one module per area; each handler self-registers
    registry.py               message type -> handler, with cancels/background flags
    narration.py              hunk narration + replies
    review_flow.py            next/prev/jump, reviewed marks, start/end/new review, refresh
    comments.py               queued review comments + finish_review document
    act_now.py                Act Now preview + confirm
    research.py               Look deeper: read-only investigation by the coding agent
    explore.py                step into, all-files browsing, explore replies
    voice.py                  voice prefs, tour read-aloud, markdown preview/read-aloud
    settings.py               settings panel
  web/                      plumbing below the handlers
    config.py                 CONFIG (config.yaml + UI overrides), logging
    runtime.py                BRIEFING/STT/TTS clients, run_llm/run_agent, send helpers
    session.py                per-connection Session state
    context.py                prompt context + token budgeting (pure functions)
    question_context.py       facts fetched per reply question (tests, callers, history, definitions)
    progress.py, speech.py    senders shared by several handler modules
  config.yaml               all runtime configuration
  services/
    conversation_service.py   live reviewer persona (Ollama/Anthropic)
    briefing_service.py       per-hunk investigative briefing + cache
    harness_service.py        coding agent: Act Now (sandbox copy, apply) and Look deeper (read-only), model from Cline settings
    acp_client.py             Agent Client Protocol (JSON-RPC over stdio) transport
    editor_service.py         "paste into your own session" request text
    review_handoff.py         Create plan: comment anchors, the review plan, the apply-review skill
    diff_service.py           git diff reading + hunk splitting
    voice_service.py          STT/TTS adapters
    session_store.py          review-progress persistence
    settings_store.py         UI model/context overrides layered over config
    preflight.py              the startup setup check
    project_overview.py       reads .context/project_overview.json
    call_map.py               reads .context/call_map.json
    changeset.py              reads the change-set themes prep-review writes
    code_search.py            grep-style code search used by briefing/etc.
    errors.py
  providers/                model adapters (ollama.py, anthropic.py) behind base.py
  prompts/                  frontier/small prompt sets
  utils/
.claude/skills/             prep Skills you run before opening the app, in
                            Claude Code or Cline (both read this path)
  project-overview/           what the project is        -> .context/
  call-map/                   who calls what (a scanner) -> .context/
  prep-review/                why each hunk exists       -> .briefing/
  judge-live-review/          maintainers only: judge this repo's live suite
static/
  index.html, style.css           the review UI (no build step — plain
  js/                              HTML/CSS/JS served as-is; js/ is ES
                                   modules, entry point js/main.js)
tests/                      pytest unit tests (import app.* directly)
qa_agent/                   Playwright UI regression suite (external)
docs/                       reference docs
```
