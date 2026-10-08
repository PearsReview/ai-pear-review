# Configuration

Every runtime setting, and what each one is for. The file itself
([app/config.yaml](../app/config.yaml)) is heavily commented — this is a
map of it, not a replacement for reading it.

Companion docs: [README.md](../README.md) (how to run it),
[architecture.md](architecture.md) (what the pieces are).

---

Every setting lives here; nothing is a hardcoded default buried in code.

To change a setting across *every* repo without editing `app/config.yaml`,
put the keys you want to override in `~/.config/pear-review/config.yaml`. It
is deep-merged over `app/config.yaml` at startup (your keys win; everything
else falls through to the defaults). The app never writes it — create it
yourself. Per-repo overrides from the settings panel still layer on top of
both (they land in `.review/ui_settings.json`). Set `REVIEW_USER_CONFIG` to
use a different file.

- **`server`** — host/port (default `127.0.0.1:8765`) and `repo_path`
  (`"."` — the directory you launch `python run.py` from).
- **`conversation`** — the live reviewer persona
  (`present_hunk`/`respond_to_reviewer`).
  - `provider: ollama` (default), `provider: anthropic`, or `provider:
    openai` (any OpenAI-compatible endpoint or gateway) — each has its own
    `model`/connection settings under its own key, so switching providers
    can never accidentally send one provider's model name to the other's API.

    **Switching to a hosted provider is two changes, not one:** set this key
    *and* provide the API key. A key on its own changes nothing — this is
    what decides who gets called. For `anthropic` the key is
    `ANTHROPIC_API_KEY`; for `openai` it is `OPENAI_API_KEY`, plus a
    `conversation.openai.base_url` naming the endpoint (include the version
    path it expects, e.g. `.../v1`) and a `model` id it routes. The settings
    panel writes the same choices per-repo, if you'd rather not edit the file.
  - `prompt_tier: auto | small | frontier` — which prompt set to use (see
    `app/prompts/`); `auto` picks frontier for `anthropic` and `openai`, and
    for `ollama` guesses from the model name.
  - `max_tokens`, `timeout_seconds` — see
    [Performance and hardware](#performance-and-hardware) before changing
    the timeout.
- **`briefing`** — `max_tokens` for the on-demand per-hunk briefing (see
  [Architecture](architecture.md)). Nothing else to configure here — it
  rides the same provider connection as `conversation`.
- **`stt`** / **`tts`** — the speech endpoints. Self-hosted is the default
  and what this is confirmed against
  ([stt_tts](https://github.com/PearsReview/stt_tts): a `faster-whisper`
  STT + `kokoro-onnx` TTS service, both served from one FastAPI app at
  `http://localhost:8000`, whose `/transcribe` and `/speech` endpoints
  match the defaults in `app/config.yaml`), but nothing here requires local: `endpoint` can
  be any URL, and `token` — set it from the settings panel, never in the
  file — is sent as an `Authorization: Bearer` header for a hosted service
  that wants one.

  What an endpoint *does* have to match is the shape
  ([voice_service.py](../app/services/voice_service.py)):

  - **STT** — `request_format: multipart` posts the recording as a field
    named `file`; `request_format` anything else posts the raw bytes as
    `application/octet-stream`. Either way the reply is JSON, and
    `response_field` (default `text`) names the key the transcript is read
    from.
  - **TTS** — `request_format: json` posts `{"text": ...}`; anything else
    posts the raw UTF-8 text. The reply is the audio bytes themselves, and
    `mime_type` tells the browser how to play them. `max_chars`/`max_words`
    split anything longer into consecutive clips.

  A hosted API whose body differs — an OpenAI-style one expecting `model`
  and `input` alongside the text — doesn't drop in; point these at a thin
  proxy of your own that translates, or at a service that already speaks
  this shape. `method` and `timeout_seconds` are per-endpoint.

  Voice input/output is entirely optional either way: if an endpoint isn't
  reachable, the corresponding feature just reports itself unavailable in
  the status bar — nothing else breaks.
- **`harness`** — the coding agent Act Now and Look deeper run through
  (`agent: none | cline`, also settable from the settings panel) and a
  per-turn `timeout_seconds`. The model and credentials are whatever you set
  up with `cline auth`: the app reads them from Cline's own settings and
  passes them on, because Cline doesn't read them itself in the mode the app
  uses. The settings panel shows which model that resolved to.
- **`debug`** — test-only. `capture_llm_calls: true` writes every model
  call's full prompt and response to `capture_dir` (default `.llm_calls/`),
  for `qa_agent/`'s judges. Leave it off: the files hold whole source files
  and conversations, and nothing prunes them.

## Performance and hardware

Speed is a property of your hardware and your model, not of this app. Every
hunk you explain costs a briefing call and a narration call, and those run
wherever you've pointed them: a local model on a GPU, the same model on CPU, and a
hosted API are three very different experiences of the same review.

**This has only been exercised on a small number of machines, against a
small number of models.** There are no benchmarks here, and any timing
you see quoted anywhere — in an issue, a commit message, a config comment —
describes someone else's setup rather than predicting yours. Try it on a
repo of your own before drawing conclusions.

One consequence worth knowing up front, because it doesn't look like a
performance problem when you hit it: a model call that outruns
`conversation.timeout_seconds` is **abandoned, not waited out**. On slower
hardware that surfaces as a hunk which never narrates rather than as one
that narrates late. If that happens, raise the timeout (and consider a
smaller model, a smaller `num_ctx`, or `provider: anthropic`).
