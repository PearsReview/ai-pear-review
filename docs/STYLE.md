# Style and extension guide

How code in this repo should read, and the two patterns it should grow
along: a new model provider, and a new piece of functionality.

This is a proposal grounded in what the code already does. Where it says
"today", that's the current shape; where it says "should", that's the
target. Nothing here requires a rewrite — see "Migration" in each part.

Companion docs: [README.md](../README.md) (how to run it),
[architecture.md](architecture.md) (what the pieces are).

---

## Part 1 — House style

### 1.1 Comments: the calibration rule

This codebase's defining habit is long "why" comments carrying real
evidence. That habit is worth keeping — it is why a reader can tell that
`num_ctx` must be set explicitly, or why a cancellation token is created
fresh per call. But it is currently unbounded: measured across
`conversation_service.py`, **53% of lines are comment or docstring and
39% are code** (`server.py`: 42% / 48%), and the same fact is often
argued from scratch in four places.

The goal is not less explanation. It is the same explanation, once.

Keep a comment when it records something a reader **cannot re-derive**
from the code:

- **A measurement.** "a ~16k-token prompt had exactly 2050 tokens
  processed" (config.yaml). Nobody can recover that by reading code.
- **An observed failure.** "caught live, on this app's own default model
  ... the grammar-constrained sampler ran far past any reasonable length"
  (`OllamaProvider.complete`). This is why the guard exists.
- **A rejected alternative.** "A shared token would be silently wrong in
  the exact case this exists for" (`arm_cancel`). It stops the next
  person from 'simplifying' it back.
- **A cross-module contract.** "returns a NEW history list rather than
  mutating the caller's" (`_send`). The signature alone doesn't say it
  matters.

Cut a comment when it:

- **Restates the code.** If the sentence is the line in English, delete it.
- **Re-justifies a decision already justified elsewhere.** Explain once,
  then cross-reference. Worked example: Ollama's silent front-truncation
  was argued from scratch in three places in `conversation_service.py`
  plus config.yaml. Moving it into `app/providers/ollama.py` kept the full
  argument (with its measurement) in config.yaml and cut the code-side
  copies to `see config.yaml's num_ctx note` — the same one `static/js/settings.js`
  already used.
- **Is changelog.** "This used to be X, then we changed it to Y" is
  git's job. Keep only the forward-looking half: *"don't go back to X —
  it caused Z."* Same fact, a third the words, and it stays true.
- **Re-states the module's premise in every section.** State a premise in
  the module docstring; below it, refer to it.

**One fact, one home.** Before writing a paragraph, search for the
phrase. If it's already explained somewhere, link instead.

**Rule of thumb:** a comment longer than the code it explains needs to be
carrying a measurement, a failure, or a rejected alternative. If it
isn't, it's noise no matter how well written.

### 1.2 Module shape

Every `app/services/` module should present:

- A docstring saying **what it owns and what it must never do** (e.g.
  `editor_service` never calls an LLM and never touches disk).
- **One public error type** deriving from `ServiceError`
  ([errors.py](../app/services/errors.py)). A module with nothing distinct
  to add re-raises rather than wrapping — `briefing_service` does this
  deliberately, and that reasoning is already recorded in errors.py.
- **Frozen dataclasses for value objects** (`Hunk`, `Briefing`,
  `PromptSet`, `Check`). Not dicts. A dict crossing a module boundary is
  a missing dataclass.
- **Module-private helpers prefixed `_`.** Anything without an underscore
  is API someone may call.

`from __future__ import annotations` at the top of every module.

### 1.3 Config and defaults

**A default value for a config key lives in exactly one place.**

The case that taught this: `conversation.provider` defaulted to
`"ollama"` in three modules but to `"anthropic"` in `server.py`'s startup
warning, so a config with no `provider` key checked Ollama for
reachability and then warned that `ANTHROPIC_API_KEY` wasn't set.
`conversation_service` had even named its own `_DEFAULT_PROVIDER` so
sites couldn't drift — and the other files re-wrote the literal anyway.
Now there is one `DEFAULT_PROVIDER` in `app/providers/base.py` that every
module imports.

The fix is structural, not disciplinary: a default belongs to the thing
that owns the key (each adapter owns its own block's defaults), and
everyone else imports it or reads the resolved value.

`.get(key, default)` scattered across call sites is how this happens.
Resolve config once, at the boundary, into a typed object.

### 1.4 Async and threading

Two rules, both already established — keep them absolute:

- **Nothing blocking runs on the event loop.** `git`, HTTP, file IO, all
  via `asyncio.to_thread`. This isn't theoretical: an unwrapped
  `get_review_hunks` froze every connection on the server
  (`websocket_endpoint` in [server.py](../app/server.py)).
- **Every LLM call goes through `run_llm`** (and every coding-agent turn through `run_agent`) ([app/web/runtime.py](../app/web/runtime.py)).
  It arms the cancellation token and logs start/ok/cancelled/failed with
  a duration. Calling `asyncio.to_thread` directly on something that
  talks to a model is a bug — that's what made the Act Now hang invisible.

If you add a second kind of long-running work, give it its own choke
point rather than hand-rolling `to_thread` at each call site.

### 1.5 Naming and size

- Handlers: `handle_<message_type>`. Senders: `send_<event>`.
  Predicates: `_is_*` / `_has_*`. Checks returning a report: `_check_*`.
- **A module over ~600 lines needs a reason.** `server.py` was 2665 until
  Part 3's split. Split along the same lines rather than let one regrow.
- Prefer a named constant to a literal *when the number has a rationale*
  (`_CONNECT_TIMEOUT_SECONDS`, `_MAX_FILE_PROMPT_LINES`). That's the
  hook the rationale hangs on.

### 1.6 Tests

- **Pure logic → `tests/`**, no server, no browser, no model. Fast, and
  the default `pytest` run stays hermetic (`tests/conftest.py` excludes
  the live suites deliberately).
- **UI/behaviour → `qa_agent/`**, driving a real browser against a real
  server.
- **Anything calling a real model is opt-in**, in its own invocation
  (`qa_agent/live/`, `tests/live_llm/`), never in the default run.

New behaviour needs a `tests/` test if it has any logic worth naming.
"It's covered by the UI suite" is only true for UI.

---

## Part 2 — Adding a model provider

### 2.1 Status

The adapter seam **is built** (`app/providers/`): `ConversationClient`
delegates every model call to a provider adapter, the Ollama/Anthropic
branches in `conversation_service.py` are gone, and `DEFAULT_PROVIDER`
has one home. What still names providers directly — the follow-ups in
2.6:

| File | Still provider-specific |
|---|---|
| `preflight.py` | `_check_model_provider` + `_check_ollama` / `_check_anthropic_key` |
| `settings_store.py` | three hardcoded `("ollama", "anthropic")` tuples |
| `static/js/settings.js` | `provider === "anthropic" ? … : …` in three places |

### 2.2 The pattern: Adapter, composed — not a subclassed client

Two different things vary independently in the conversation layer:

- **Conversation policy** — history is returned, never mutated; prompt
  assembly; prompt-tier selection; usage counters; call capture. Same for
  every model.
- **Transport** — Ollama's streamed `/api/chat` with `num_predict` and
  `num_ctx`; Anthropic's SDK call. Different per model.

So there are three roles, each with its own mechanism:

| Role | Mechanism | Where |
|---|---|---|
| Translate a vendor API into one call | **Adapter** | `OllamaProvider`, `AnthropicProvider` |
| Declare what an adapter must provide | **Thin abstract base class** | `ChatProvider(ABC)` in `base.py` |
| Connect policy to transport | **Composition** | `ConversationClient._provider` |

**Why not inherit the client** (`OllamaConversationClient(ConversationClient)`):
the policy — the well-tested part — would become a base class every
provider could override, and any second axis (a briefing-specific client,
a test fake) multiplies classes. Composition keeps policy written once.

**Why an ABC rather than a `Protocol`:** a provider missing `complete`
fails when it's constructed, not on first use, and the registry holds
explicit subclasses that a test can check (`tests/test_providers.py`).

**One level deep.** Every concrete provider subclasses `ChatProvider`
directly — enforced by `test_providers_are_one_level_deep`. An
OpenAI-compatible server (vLLM, LM Studio, OpenRouter) is *one* adapter
configured by `base_url`, not a subclass tree.

```python
# app/providers/base.py (abridged)
class ChatProvider(ABC):
    name: str
    capabilities: Capability = Capability.NONE

    def __init__(self, provider_config: dict, timeout: float) -> None: ...

    @classmethod
    @abstractmethod
    def available(cls, provider_config: dict) -> bool: ...

    @classmethod
    def list_models(cls, provider_config: dict) -> list[str]: ...  # MODEL_LISTING only

    def prepare(self) -> None: ...  # optional one-time, networked setup

    @abstractmethod
    def prompt_tier(self) -> str: ...  # "small" | "frontier" for this model

    @abstractmethod
    def complete(self, messages, system, max_tokens, response_schema, cancel) -> Completion: ...
```

**Adapters adapt to the model, not just the vendor.** `prepare()` runs once
when `ConversationClient` is built — off the event loop, because
`server.py` constructs it via `asyncio.to_thread` — and is where an adapter
may ask its server what the configured model is. `OllamaProvider` uses
`/api/show` so any model Ollama serves can be configured:

| What it learns | What it does with it |
|---|---|
| capabilities lack `completion` | refuses the model (embedding-only), and `list_models` hides it |
| capabilities include `thinking` | sends `think: false`; gpt-oss, which can't disable it, gets `"low"` plus a token allowance |
| native context length | caps `num_ctx`, so prompt budgeting matches what the model can attend to |
| parameter count | `prompt_tier()`: 60B+ → frontier, with the model name as fallback |

`prepare()` tolerates an unreachable server — the adapter then behaves as
it would without the lookup — and raises only for a definitive "this model
can't be used". Anything it learns must be settled before the first read of
`num_ctx` or `prompts`, which is why it isn't lazy.

Each adapter receives only its own config block (`conversation.ollama`,
`conversation.anthropic`), so one provider's model name can never reach
another's API. `build_provider` looks the name up in `REGISTRY` and
raises `ConversationError` listing the valid names for an unknown one.

### 2.3 Capabilities, not provider checks

The asymmetries between providers are real and are *declared* on each
adapter rather than discovered by reading branches:

| | Ollama | Anthropic |
|---|---|---|
| `CANCELLATION` — stop mid-flight | yes (streamed) | no |
| `JSON_SCHEMA` — constrained output | yes | no (prompt + `extract_json_object`) |
| `CONTEXT_WINDOW` — caller sets it | yes (`num_ctx`) | no |
| `MODEL_LISTING` — offer installed models | yes (chat models only) | no |

Callers ask about capability, never identity:

```python
# ConversationClient._complete
if not self._provider.capabilities & Capability.JSON_SCHEMA:
    response_schema = None
```

Add a capability flag only when something reads it — a declared but
unused flag is exactly the drift 1.3 warns about. `MODEL_LISTING` is read
by `handle_get_settings`, which no longer names any provider.

### 2.4 The one choke point

Every call — narration, replies, explore answers, briefings —
goes through `ConversationClient._complete`, which in order:

1. refuses a call already cancelled before dispatch;
2. drops `response_schema` if the adapter lacks `JSON_SCHEMA`;
3. calls `provider.complete(...)`;
4. captures the call (test-only `debug.capture_llm_calls`);
5. raises on empty text, then counts usage.

Adapters therefore contain transport only. Anything that must be true for
*every* provider belongs in `_complete`, not in each adapter.

### 2.5 Checklist: adding a provider

1. New module in `app/providers/` subclassing `ChatProvider`: set `name`
   and `capabilities`; implement `available`, `prompt_tier` and
   `complete`; override `prepare` / `list_models` if the provider can
   describe its models. Raise `ConversationError` (or
   `ConversationCancelled`) on failure.
2. One line in `REGISTRY` (`app/providers/__init__.py`).
3. A config block under `conversation:` in `config.yaml`, keyed by `name`.
4. A column in the capability table above.
5. Run `pytest tests/test_providers.py` — the registry tests pick the new
   adapter up automatically; add an adapter test with a faked SDK/HTTP
   client, same shape as `tests/test_conversation_service_anthropic.py`.
6. Until 2.6 lands, also: a `_check_*` in `preflight.py`, the tuples in
   `settings_store.py`, and the settings panel in `static/js/settings.js`.

### 2.6 Remaining migration (in this order)

Done: prompt tier (`ChatProvider.prompt_tier`, with `prompt_tier:
small|frontier` in config still the explicit override) and model listing
(`Capability.MODEL_LISTING`).

1. **Preflight onto the adapter** — `preflight(provider_config) -> list[Check]`
   per adapter; `_check_model_provider` becomes a registry lookup. This is
   also where "configured model can't chat" should surface for a first-time
   user — today it's only logged when a connection opens.
2. **Settings** — a per-adapter settings schema drives `settings_store`'s
   allowlist and the settings panel.

When these land, step 6 of the checklist disappears.

---

## Part 3 — Adding functionality

### 3.1 Status

**Built.** Every WebSocket message type is a registered handler in
`app/handlers/`, and `websocket_endpoint` dispatches through
`app/handlers/registry.py`. `tests/test_handler_registry.py` asserts the
registry covers exactly the message types the frontend sends.

### 3.2 The pattern: a handler registry

```python
@handler("act_now", cancels=True, background=True)
async def handle_act_now(ws: WebSocket, session: Session, payload: dict) -> None: ...
```

- `cancels` — call `cancel_current` first.
- `background` — wrap in `create_task`, assign to `session.current_task`.

Every handler takes `(ws, session, payload)`, unused or not, so dispatch
never special-cases a signature.

`background` wraps the *whole* handler. A handler that must change session
state before its task starts stays inline and creates the task itself:
`next`/`prev` bump `session.index` synchronously, because inside a task a
second quick click would cancel the first before its increment ran
(`test_rapid_next_clicks_each_advance`).

Not built yet: declarative `requires=` preconditions. Guards
(`if session.review_ended`, `if not session.review_started`) are still
written inside each handler, and whether an unmet guard errors or silently
no-ops still varies. Unifying that is a behaviour change, so do it as its
own step.

### 3.3 Layout

```
app/server.py        FastAPI app, Origin check, connect, dispatch loop
docs/wire-protocol.md  the message contract, both directions
app/handlers/        one module per area; importing the package registers everything
    registry.py      narration.py  review_flow.py  comments.py
    act_now.py       research.py   explore.py      voice.py
    settings.py
app/web/             plumbing below the handlers
    config.py        CONFIG, logging
    runtime.py       BRIEFING/STT/TTS, run_llm, send_json/send_error, cancel_current
    session.py       Session
    context.py       prompt context + token budgeting (pure functions)
    question_context.py  facts fetched per reply question
    progress.py      review_progress / summary screen senders
    speech.py        try_speak
```

Import direction is `server -> handlers -> web -> services/providers/prompts/utils`,
checked by `tests/test_layering.py`. A helper that two handler modules need
goes down into `web/`, not sideways into another handler module. The one
sideways import is a handler calling another area's *function*, e.g.
`act_now` -> `review_flow.refresh_diff`.

Read `STT`/`TTS` as `runtime.STT`/`runtime.TTS`: `handle_set_settings`
rebinds them, and a `from ..web.runtime import TTS` would keep the client
that existed at import time.

### 3.4 Checklist: adding a message type

1. A handler in the right `app/handlers/` module, decorated with `@handler`.
2. Declare `cancels` / `background` rather than calling `cancel_current` or
   `create_task` by hand (except the inline-state case in 3.2).
3. Send it from the frontend. Explain the payload and how it behaves in the
   handler docstring; add a row to [wire-protocol.md](wire-protocol.md) with
   the name, payload keys and handler, and nothing else. The contract goes in
   the table, the reasoning goes next to the code — the two drifted apart
   once already when both lived in server.py.
4. Add a `tests/` test if it has logic, and a `qa_agent/` test if it has UI.
   `test_every_frontend_message_has_a_handler` fails until 1 and 3 agree, and
   `test_wire_protocol_doc_matches_the_registry` fails until the table does.

---

## Part 4 — Making it stick

### 4.1 Tooling

**Ruff is configured and runs in CI.** The ruleset lives in
[pyproject.toml](../pyproject.toml); `ruff check .` is clean, and
[.github/workflows/ci.yml](../.github/workflows/ci.yml) fails the build if
that stops being true. `pip install -r requirements-dev.txt` gets you the
same pinned version CI uses.

Three choices in that config are worth knowing before you change them:

- **`target-version = "py310"`, not `py313`.** It has to match the floor in
  `requires-python`, because that floor is what CI proves on every push. Set
  to `py313` and a `UP` rule will happily rewrite something into 3.11+ syntax
  that passes here and fails the 3.10 leg.
- **`RUF001`–`RUF003` are ignored.** They flag typographic quotes and dashes
  in strings, which this codebase uses deliberately in narration text,
  spoken-form rules and user-facing messages. Flagging every one is the false
  positive that teaches people to stop reading the output — the same failure
  1.1 warns about and `tests/test_style_violations.py` exists to prevent.
- **`SIM105` and `RUF005` are ignored** as preference, not principle.
  `try/except/pass` → `contextlib.suppress` would mean editing process and
  socket teardown for cosmetics; `[first] + rest` → `[first, *rest]` is not
  clearer.

**`BLE` (blind `except`) is on for app code** and off for `tests/`,
`qa_agent/` and `.claude/`, which are cleaning up, probing or reporting,
where the specific failure genuinely doesn't matter. App code has one
suppression, in `preflight.py`, carrying its own reason. A new one needs
the same: say why *any* failure is the right thing to catch there.

**`N` (pep8-naming) is deliberately not selected.** `visit_FunctionDef`
and its siblings are `ast.NodeVisitor`'s dispatch API, not a naming choice.

**Formatting is `ruff format`, and CI enforces it.** Run `ruff format .`
before committing; `ruff format --check` fails the build otherwise.
`line-length = 120` rather than 100 because that is how the code was
actually written: at 100 the first format pass rewrapped ~480 lines into
~1,900 extra, at 120 the net growth was ~380. The whole-repo pass landed as
one isolated commit, listed in `.git-blame-ignore-revs` so `git blame`
looks through it.

**Type checking runs in CI.** `[tool.mypy]` in
[pyproject.toml](../pyproject.toml) scopes it to `app/services/` and
`app/providers/` — the model-calling seam, where a wrong type reaches an
external API rather than a traceback, and the layer `test_layering.py`
already keeps free of web/session knowledge, so it checks standalone. Bare
`mypy` picks the scope up from there; `ruff check .` and `mypy` are both
clean.

It runs as its own job rather than beside ruff, because it needs the app's
real dependencies installed. With `ignore_missing_imports` and no
`anthropic` package present, that import resolves to `Any` and the check
passes while verifying nothing — worse than not running it at all.

Getting from 34 findings to zero took five edits and no design decisions,
and each one left the code clearer. Two are worth knowing about because
they are the pattern you will hit again:

- **Don't splat a dict into a typed SDK call.** `messages.stream(**request)`
  erased which argument was which, and all four then failed against every
  overload — 19 of the 34 findings from one line. Passing the keywords
  explicitly fixed all of them.
- **Truthiness through a wrapper doesn't narrow.** `bool(command) and
  command[0]` still reads as `list[str] | None` to mypy; so does calling
  `settings.get("auth")` twice in one ternary. Assign once, then guard.

Two `cast`s survive, both at an SDK boundary and both commented: plain
message dicts into `MessageParam`, and Ollama's request payload. A cast at
a boundary you control on one side only is honest; one used to silence a
checker inside your own code is not.

### 4.2 Registry completeness tests

The point of both registries is that they can be checked. The provider
one already is — `tests/test_providers.py` asserts every `REGISTRY` entry
subclasses `ChatProvider` directly, names itself correctly and leaves no
abstract method unimplemented. The handler registry (Part 3) is too:
`tests/test_handler_registry.py`'s `test_every_frontend_message_has_a_handler`
parses every `static/js/*.js` for `send("...")` calls and asserts the set equals
`HANDLERS`. Without it, a frontend/backend mismatch surfaces only at
runtime, as an "Unknown message type" error, and only on that path.

### 4.3 Definition of done

- [ ] `ruff check .` is clean and `ruff format .` has been run (4.1). CI
      enforces both, so neither is optional.
- [ ] Public behaviour has a test at the right level (Part 1.6).
- [ ] No new provider or message-type branch outside its registry.
- [ ] New comments pass the calibration rule (Part 1.1); no fact explained
      twice.
- [ ] Config defaults added in one place only.
- [ ] Docs naming a function or file were checked to still be true (4.4).

### 4.4 Docs are code

A doc that names a function, flag or file is code. If you rename or delete
the thing, grep the docs and comments for it. [architecture.md](architecture.md)
once went stale this way — still describing a CLI-based briefing agent
and linking planning docs that had been deleted — which is exactly what
1.1's "one fact, one home" is meant to prevent: an explanation that
outlived the thing it explained.
