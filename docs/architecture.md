# Architecture — agents and startup

This is a reference doc: it describes what actually exists today and what
happens when you run it. For setup and day-to-day use, see
[README.md](../README.md).

## Two different kinds of "Claude" in this project — don't conflate them

1. **Your own Claude Code session** (whatever's open in your editor or
   terminal) — used to *build* the app, and to run the optional prep
   Skills in `.claude/skills/`, which write files the app later reads. It
   is never invoked by the running app itself.
2. **Runtime components** — code inside `app/` that runs when you
   `python run.py` and open the review UI. Each has its own connection,
   its own tool access, and a distinct job — see below.

## Runtime components

### Conversation agent — [app/services/conversation_service.py](../app/services/conversation_service.py)

- **Connection:** `conversation.provider` in
  [app/config.yaml](../app/config.yaml) — a local Ollama server (default, no
  key) or the Anthropic Messages API (`ANTHROPIC_API_KEY`, metered
  billing). Provider adapters live in [app/providers/](../app/providers/).
  Never the `claude` CLI.
- **Tool access:** none, by design — that's what keeps a reply a single
  model call rather than an agentic tool-use loop, which is the difference
  between a chat turn and a wait.
- **Job:** the presenter/reviewer dialogue — narrating each hunk and
  answering replies. One `ConversationClient` per WebSocket connection;
  the client itself holds no message history. History is scoped per hunk
  (or per file, for a file explored outside any hunk) and lives on the
  connection's `Session` ([app/web/session.py](../app/web/session.py)).
- **If unavailable** (unreachable Ollama, missing API key): the app runs in
  a degraded mode rather than refusing the connection — see below.

### Briefing — [app/services/briefing_service.py](../app/services/briefing_service.py)

- **Connection:** the same provider connection as the conversation agent;
  nothing separate to configure.
- **Tool access:** none. It never touches the working tree.
- **Job:** once per explained hunk, produces a small `Briefing` — intent /
  alternatives considered / risk notes / confidence. Narration uses it as
  extra grounding only when `confidence == "high"`, and never injects
  `risk_notes`: it's generated and cached, but it's the least grounded
  field, and a small model fills a suggestively-named slot with
  speculation that narration then repeats as fact.
- **Cached:** in memory (`session.briefings`) and on disk under
  `{repo}/.briefing/`, validated against a hash of the hunk's exact diff.
  The optional `prep-review` Skill writes deeper, tool-assisted briefings
  into the same cache.
- **If unavailable** (no connection, timeout, bad output): falls back to
  `Briefing.unavailable()` and narration runs from the raw diff alone. Not
  fatal.

### Write-capable flows — deliberately not an agentic editor on your working tree

- **Act Now** — [app/services/harness_service.py](../app/services/harness_service.py).
  Handed to the reviewer's own coding agent (Cline) over the Agent Client
  Protocol ([app/services/acp_client.py](../app/services/acp_client.py)). The
  agent brings its own model and credentials, and has tools only inside a
  temporary copy of the repo: commands, fetches and paths outside the copy
  are refused, and what it changed is previewed in the UI
  ([app/handlers/act_now.py](../app/handlers/act_now.py)). Only the
  reviewer's explicit confirm writes to the working tree — all previewed
  files or none, refused if any changed since.
- **Create plan** (the `finish_review` message) — [app/handlers/comments.py](../app/handlers/comments.py)
  and [app/services/review_handoff.py](../app/services/review_handoff.py).
  Never calls any LLM and never edits code. It builds a structured review in
  memory (each comment with the diff lines it covers, nearby context and its
  location, checked against the current files) and writes one markdown plan
  rendered from it, `.review/review_<timestamp>.md` (and, if asked, the same
  plan as the `apply-review` skill under `.claude/skills/`), for the reviewer
  to hand to their own agent session, where the edit is made by a tool they
  already trust and directly control.

### Look deeper — [app/handlers/research.py](../app/handlers/research.py)

The same coding agent as Act Now, answering one question about the code
instead of making a change. It's read-only at every layer rather than by
instruction: Cline's read-only "plan" mode, a client that advertises no
write capability, and a permission policy allowing only reads, searches
and read-only git commands inside the copy (which, unlike Act Now's,
includes the repo's history). Its answer is added as its own turn in that
hunk's conversation.

The app never automates the `claude` CLI: automating its subscription auth
is an unresolved ToS question, so anything subscription-based happens only
in your own interactive session.

## Degraded mode

The conversation agent is the only piece that's actually required — the
briefing, STT/TTS and Act Now all degrade on their own. If
`ConversationClient` construction fails for a connection:

- `session.conversation` is `None` instead of the connection being closed.
- Hunks show the raw diff with a `"system"`-role placeholder message
  instead of narration; the briefing call and TTS are skipped.
- Replies are rejected with a clear error instead of silently doing
  nothing.
- The initial `service_status` message's `llm` flag reflects this
  connection's actual state.

## Session persistence

[app/services/session_store.py](../app/services/session_store.py) writes
review progress to `{repo}/.review/session_state.json` —
`review_started`/`review_ended`, which hunks are reviewed, and queued
review comments — continuously overwritten, reloaded by every new
connection. Reviewed status is keyed by a hash of the hunk's own content
(`stable_hunk_key`, not its positional index), so it survives a fresh
`git diff` read across a restart as long as the hunk's lines are
unchanged, regardless of what shifted around it.

Per-hunk briefings have their own separate durable cache
(`{repo}/.briefing/`, content-hash validated). Narration text and
conversation history are *not* persisted — re-narrating a revisited hunk
after a restart is an accepted one-time cost, not a correctness gap.

## What happens on startup

**`python run.py`:**

1. `load_dotenv()` reads `.env` if present, so a project-scoped
   `ANTHROPIC_API_KEY` becomes visible to this process only (see
   [.env.example](../.env.example)).
2. `from app.server import app` triggers the module-level setup in
   [app/web/config.py](../app/web/config.py) and
   [app/web/runtime.py](../app/web/runtime.py), once per process:
   - `config.yaml` is loaded and overlaid with any settings saved from the
     UI (`.review/ui_settings.json`). A non-loopback `server.host` logs a
     warning.
   - `BriefingClient`, `STTClient` and `TTSClient` are constructed — no
     live calls yet.
   - `check_conversation_available(...)` — a cheap probe of the configured
     provider. Logs a warning if it fails; does not block startup.
3. The preflight ([app/services/preflight.py](../app/services/preflight.py))
   checks git, the repository, whether there are changes, and the model
   provider (Ollama reachable and model installed, or API key present),
   and prints a report. Only a `FAIL` (no git, not a repository) stops
   startup.
4. A free port is chosen (the configured one, or the next free one),
   `uvicorn` starts, and a browser tab opens unless `REVIEW_NO_BROWSER` is
   set. No LLM calls have happened yet.

**Browser opens a WebSocket to `/ws`** (`websocket_endpoint` in
[app/server.py](../app/server.py)):

5. The handshake is rejected unless its `Origin` matches its `Host`.
6. `get_review_hunks()` reads the diff against HEAD (staged + unstaged,
   plus untracked files). If git itself fails, the connection is closed
   with an error.
7. `ConversationClient` is constructed **for this connection** — a
   missing key or unreachable server surfaces here as `ConversationError`
   and falls into degraded mode.
8. Persisted review state (`.review/session_state.json`) is reloaded, and
   `service_status` and review progress are sent.

**Reviewer starts the review / moves to a hunk:**

9. By default nothing is called: the hunk shows its diff and an
   **Explain** button (`explain_hunk`). A hunk is narrated when the
   reviewer clicks it, or on arrival if **Explain changes** is set to
   *Automatically* (`set_narration_prefs`, `session.auto_narrate`). A hunk
   already narrated in this connection is re-sent from memory either way.
10. When a hunk is narrated and the conversation agent is available:
    briefing read from cache or computed → narration → text sent to the
    browser → TTS attempted.
11. If the agent is unavailable: raw diff with a placeholder system
    message; no briefing, no TTS.

**Reviewer replies:**

12. If audio was sent instead of text, STT runs first.
13. If the conversation agent is unavailable, the reply is rejected with
    an error.
14. Otherwise the reply is answered from that hunk's history, plus
    question-specific facts gathered by
    [app/web/question_context.py](../app/web/question_context.py) (tests,
    callers, line history, definitions).
