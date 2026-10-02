# qa_agent — UI regression suite

A Playwright-driven pytest suite for the AI Pear Review app.
Deliberately isolated from the app itself:

- Nothing here imports `app.*`, and the app never imports `qa_agent`.
- It only ever talks to the running app the way an external actor would —
  driving a real Chromium browser against a real `python run.py`
  subprocess. Stage 2/3's judges use their own independent Ollama client
  (`llm_client.py`) rather than reusing
  `app/services/conversation_service.py`'s — a brand-new HTTP request per
  judgment, never sharing "conversation session" state with whatever the
  app-under-test's own `ConversationClient` is doing, and always targeting
  the model `app/config.yaml` actually configures (see `conftest.py`'s
  `judge_model_config` fixture) rather than a constant that can drift out
  of sync with it.

## Setup (one time)

```
pip install -r requirements-dev.txt
python -m playwright install chromium
```

## Running

```
pytest qa_agent/
```

Starts `run.py` once for the whole session, pointed at a small throwaway
git repo (`conftest.py`'s `scratch_repo` — created fresh under pytest's
own tmp dir, never inside this actual project) so every run is
deterministic regardless of whatever's actually uncommitted in the real
working tree at the time. It runs from a **full copy** of `app/`,
`static/`, and `run.py` under pytest's own tmp dir (`app_copy_dir`), with
only the copy's `config.yaml` patched to point `repo_path` at the scratch
repo — the real project's own files are never written to at all. (An
earlier version of this fixture patched the real `app/config.yaml` in
place and restored it on teardown; a hard-killed test process can't run
that restore step, and this once actually left the real config.yaml
pointing at a deleted temp path. The copy-based approach has no such
failure mode.)

Each test gets a fresh server-side `Session` for free (the app builds a
new one per WebSocket connection, no reattachment), so reviewed-hunk
state, queued comments, and conversation history never leak between
tests. A test whose flow performs a real disk write (Act Now confirm)
should depend on the `reset_scratch_repo` fixture to restore the working
tree afterward.

### The generated-repo suite

```
pytest qa_agent/generated/                      # fixed default seed
pytest qa_agent/generated/ --repo-seed=random   # a new repo shape each run
pytest qa_agent/generated/ --repo-seed=2409333  # reproduce a specific run
```

A **separate pytest invocation**, against a repo built per-seed by
`qa_agent/repo_gen.py` instead of the hardcoded `scratch_repo`. The seed is
printed at session start, so a failing random run is reproducible with one
flag.

It's deliberately not merged into `pytest qa_agent/`, for two reasons:

- **The two suites do different jobs.** The fixed repo is a regression
  baseline — a failure there means the app broke. The generated repo is a
  robustness probe — a failure there might instead mean a *test* made an
  assumption that only held for one repo shape. Merging them makes every
  red run ambiguous.
- **Port 8765 is a session-scoped singleton.** `app_server` binds it for
  the whole session, so a second server in the same session would collide.
  The generated suite binds a free port instead, which also means it can
  run while the other suite (or your own `python run.py`) is up.

`repo_gen.generate_repo(root, seed)` returns a `RepoSpec` carrying
everything a test would otherwise hardcode: files, which are changed, hunk
counts per file, edited symbol names, folders. Structure varies per seed,
not just vocabulary — file count, which files change, how many hunks each
gets, and folder depth are all drawn from the seed (about 15 distinct
shapes over 120 seeds).

Two design points worth knowing before adding tests here:

- **Hunk counts are measured, never predicted.** `git diff --unified=3`
  merges edits within ~7 lines of each other, and the exact threshold
  depends on surrounding content — so the generator writes the repo, runs
  the real `git diff`, and counts what came out. The spec is true by
  construction, and `_validate` refuses to hand out a repo whose hunks got
  merged unexpectedly, rather than letting ~60 tests time out one by one.
- **Never let the spec be both sides of an assertion.** Comparing a spec
  value against something else derived from the same spec passes by
  construction and tests nothing — this is the specific way a
  content-agnostic test rots. Every assertion compares something the *app*
  produced (DOM, WebSocket frame) against the spec as oracle.

`tests/test_repo_gen.py` pins the generator's own invariants (spec matches
git, no untracked files left behind, both sides parse as Python, the
padding that keeps multi-hunk files separate still works).

#### The narration pack — recording instead of judging

`test_narration_quality.py` walks every hunk, then writes
`qa_agent/results/generated_narration_pack.md`: per hunk, the diff, what
the app said, **the briefing that produced it**, and the verbatim prompt
the model received — for a Claude Code session (or a human) to assess.

There is deliberately **no LLM judge in this loop**, unlike
`test_semantic_quality.py` next door. Two reasons:

- **The existing judge is the model it judges.** `judge_model_config` reads
  the app's own `conversation.ollama` block, so `qwen2.5-coder:7b` grades
  its own output and shares its blind spots. `judge_with_voting`'s `k=3`
  exists largely to paper over how unreliable that is — three calls per
  verdict to compensate.
- **A verdict in a JSONL fixes nothing.** Handing the judgment to a session
  that can also *change the prompt* is the difference between labelling a
  problem and closing it. This is the same split the `.claude/skills/`
  already use: cheap deterministic work in the harness, hard thinking in a
  session, meeting at a file.

Recording the **briefing** alongside the narration is what makes the pack
diagnostic rather than merely descriptive. Two model calls run in a chain —
briefing writes notes, narration is handed them and speaks — so a note the
*briefing* invented looks exactly like the narration lying, and you go and
fix the wrong prompt. This caught a real one on its first run: a briefing
claiming `max()` "could have a higher time complexity", repeated to the
reviewer as fact.

**The counts are rates, not pass/fail.** The behaviour is probabilistic:
four runs of one identical seed gave form breaches 1 → 0 → 1 → 0. Only two
checks assert — a symbol attributed to the wrong file (severe, never yet
seen) and an incomplete walk. Everything else is counted, because a test
that goes red two runs in three gets muted, and what's worth tracking is
whether a prompt change *moves the number*.

`tests/test_review_pack.py` pins the flag logic, especially the false
positives: `foreign_symbols` gates an assertion, so a wrong flag would fail
the suite on a perfectly good narration.

`test_act_now.py` and `test_semantic_quality.py` call the real Ollama
model configured in `app/config.yaml` and can take real seconds per
test — that's expected, not a hang.

**The long-combined-run flakiness is fixed** (was: `pytest qa_agent/`
end-to-end occasionally hit timeouts in `test_semantic_quality.py` /
`test_act_now.py` that didn't reproduce when those files ran alone). The
resource-contention read was right: every test opens a connection that
fires a briefing+narration pair, then closes the browser mid-flight, and
those abandoned calls used to keep running — `asyncio.to_thread`
cancellation doesn't stop the worker thread — holding their slot in
Ollama's serialized per-model queue until a later test's request queued
behind several of them. Ollama calls are streamed now and poll a
cancellation token between chunks, so an abandoned call drops its
connection and Ollama stops generating (see `app/providers/ollama.py`'s
`OllamaProvider.complete` and `app/web/runtime.py`'s `run_llm`). A second, unrelated bug in
these files' own retry loops — re-sending without re-checking the one-shot
`#act-now-checkbox`, so retries went out as plain chat replies and waited
out the full timeout — is fixed too. Full combined runs pass.

**A second contamination source is fixed too:** `run.py` opened the UI in
the real default browser on every start, so `app_server` was silently
running with an extra WebSocket client attached. The server cancels the
previous client's in-flight LLM work whenever a new one connects, so that
tab could cancel the very briefing a test was waiting on — reproduced
directly while verifying the project-overview injection, where it showed up
as `llm[1] briefing hunk 0 cancelled after 1.6s` and no narration at all.
`app_server` now launches `run.py` with `REVIEW_NO_BROWSER=1`; the flag is
test-only and a normal `python run.py` still opens the browser as before.
Worth knowing when driving the app by hand: if you script it, set that
variable, and check the log for a single `connection open`.

**A third one, client-side rather than server-side:** the guided tour
(static/js/tour.js) auto-opens once per browser for a first-time reviewer, and
every page here is a *fresh* browser with empty localStorage — exactly the
condition its own auto-start logic looks for. Left unhandled, the tour's
full-viewport overlay would compete with every test's own `page.click()`
calls a few hundred milliseconds in. `conftest.py`'s `_suppress_auto_tour`
pre-seeds the tour's "seen" flag via `page.add_init_script(...)`, called by
the shared `page`/`page_with_ws` fixtures automatically, and by hand
wherever a file builds its own raw page instead (there's no browser-level
hook to catch every `new_page()` call automatically — see that function's
own docstring for the current list and the reminder to extend it).
`qa_agent/test_guided_tour.py` is the one file that deliberately doesn't
suppress it everywhere, since proving the auto-start actually fires is
part of what it tests.

When a run *does* fail, `conftest.py` now prints the app server's log tail,
including the `llm[N] <label> start|ok|cancelled|failed` lines with
durations. `cancelled after` under a second is healthy — it means an
abandoned call was aborted promptly.

### The live suite

```
pytest qa_agent/live/ --target-repo path/to/any/repo
```

A third **separate pytest invocation**, against a real external repo's
real uncommitted changes rather than a scratch or generated one — see
[`qa_agent/live/README.md`](live/README.md) for what it covers, the
packs it writes, and the `judge-live-review` skill that gives its
recorded verdicts a second, Claude-authored opinion after a run.

## Aggregating findings

After a run, `python -m qa_agent.aggregate_findings` reads
`findings.jsonl` (and `pytest_summary.json`, if present) and writes
`qa_agent/results/results.md` — a categorized, prioritized summary of
what the LLM judges (Stage 2/3) actually found, findings grouped by
category and sorted worst-first, plus Stage 1's pass/fail counts folded
in. Cheap and standalone (no server, no browser, no LLM call), safe to
always run after the suite, pass or fail:

```
pytest qa_agent/
python -m qa_agent.aggregate_findings
```

## What's covered

**Stage 1** (`test_accessibility.py`, `test_act_now.py`,
`test_empty_states.py`, `test_inline_comments.py`, `test_marking.py`,
`test_navigation.py`, `test_theme.py`) — mechanical checks: navigation
and its disable-while-in-flight behavior, the manual theme toggle,
double-click/keyboard line marking, the inline GitHub-PR-style
review-comment flow end to end (composer, badges, edit/remove/finish —
including their confirmation dialogs), Act Now's preview/confirm/discard
split, core accessibility properties (focus-visible, aria-label/pressed,
tab order), and the empty-state messaging in the code view.

**Stage 2** (`test_semantic_quality.py`) — semantic judging: whether
`present_hunk`'s narration, `respond_to_reviewer`'s replies, and Act
Now's proposed edits actually make sense given the diff they're
supposed to describe, via a separate isolated LLM session
(`llm_client.py`) voting 3x per case (`judge_prompts.py`'s
`judge_with_voting`) rather than trusting a single small-model response.
These tests always pass — they record a verdict/reason/votes to
`findings.jsonl` (`findings_log.py`), they never assert on it directly;
deciding what's actually worth caring about is Stage 4's job (below).

**Stage 3** (`qa_agent/scenarios/`) — realistic, hand-scripted navigation
and conversation scenarios, judged the same way Stage 2 is. This is a
deliberate divergence from the original Stage 3 design (see "Not yet
built" below): rather than an LLM agent deciding its own click/type
sequence turn by turn, every scenario here is a fixed Playwright test
function — same idiom as Stage 1 — that happens to combine multiple
actions (navigation + a live conversation) and/or judge a whole short
transcript instead of one Q/A pair. The original "agent decides its own
actions" design is *still* deferred; this suite covers different, lower-
risk ground (breadth of realistic scenarios) without taking on that
design's own flagged risk (a small model reliably outputting structured
actions turn after turn). Covers: a conversation surviving a rapid
Next/Prev double-click, switching files mid-draft (an open inline-comment
composer/Act-Now preview/explore mode all get cleared, per `onPresenting`
in `static/js/ws.js`), interrupting an in-flight reply and confirming the
session recovers, revisiting one of several hunks without a duplicate
transcript turn or replayed narration (`richer_scratch_repo` in
`conftest.py` — the default `scratch_repo` stays single-hunk, since
several existing Stage 1 tests depend on that; opting in instead gets 4
hunks across 3 changed files spread over 2 folders, alongside the
always-present zero-diff `pkg/`/`src/`/`docs/` folders, so a scenario has
real files/folders to click between rather than just one hunk in one
file), never claiming a plain-chat
reply applied an edit (`scenario_helpers.py`'s
`assert_no_edit_claim_language` is a deterministic pre-filter layered on
top of the LLM judge, same "hard check plus judge" precedent
`test_semantic_quality.py`'s `_assert_only_target_region_changed`
established for Act Now), yielding after being overruled once rather than
repeating an objection a third time, and an explore-mode question getting
a reply actually grounded in the unchanged file's content (not judged
against `REPLY_JUDGE_*`'s diff-consistency rubric, which doesn't apply
when there's no diff — see `judge_prompts.py`'s `EXPLORE_REPLY_JUDGE_*`).

**Judged categories.** `narration`, `reply` and `act_now` (Stage 2) and
`edit_claim`, `overrule`, `explore_reply` (Stage 3) are joined by four more
in `scenarios/test_response_quality.py` and `scenarios/test_review_artifacts.py`:

- `grounding` — is every claim about the code actually supported by the
  diff? `reply`'s own rubric is about *relevance*, which a fluent, entirely
  invented answer can satisfy; this asks the narrower factual question.
  Added because that exact failure was recorded here (a reply explaining
  "optimizing database queries" for a diff that only added `farewell()`).
- `style` — spoken form: no code fence, list, heading, speaker label,
  announcing preamble, or wall of text. Paired with a hard deterministic
  check (`scenario_helpers.style_violations`) for the unambiguous breaches,
  since a narration recorded here once began ``Sure, here's the hunk: ```
  @@ -1,2 +1,5 @@``. Tone and backticked identifiers are explicitly *not*
  violations — the rubric had to be tightened after it flagged a perfectly
  clean narration for "formal language".
- `marked_context` — selecting lines injects them into the next reply's
  prompt server-side (`_marked_lines_context`), invisibly: the reviewer's
  own message stays a bare "why this?". The only evidence it worked is
  whether the answer is about those lines. Mark lines that are actually
  part of the diff — marking untouched context asks a genuinely ambiguous
  question and judges nothing useful.
- `finish_review_doc` — the hand-off document is assembled prose meant for
  another engineer or AI session; `test_inline_comments.py` proved the
  queue empties and a file appears, nothing checked the document still
  contained the comments.

**Stage 4** (`aggregate_findings.py`) — reads `findings.jsonl` and, if
present, `pytest_summary.json` (written by `conftest.py`'s
`pytest_sessionfinish` hook — Stage 1's mechanical pass/fail counts, no
`pytest-json-report` dependency needed for it) and writes
`qa_agent/results/results.md`: findings grouped by category, each group
sorted by "no"/"unsure" frequency (worst category first), near-duplicate
failure reasons collapsed, and each weak finding carrying a collapsed
"what the model was given" block (see "Provenance" below). See
"Aggregating findings" above for how to run it.

## Provenance: what the model was actually given

A verdict is only as good as the record behind it, so every judged finding
carries both sides of the exchange:

- `judge` — which model ruled and the exact prompts it ruled on.
- `llm_call` — the **app's own** call: verbatim system prompt, the full
  message list (so multi-turn history is visible, not just the last
  question), the response, and token counts.

The app writes those captures itself, because nothing external can see
them: `conversation_service.py` — the path behind narration, replies and
explore answers — logs no prompt content and writes nothing to disk, and
reconstructing the prompts inside `qa_agent` would duplicate the app's own
assembly logic and drift out of sync with it. So `app/config.yaml` gained
a **test-only** `debug.capture_llm_calls` flag, **off by default**:
`conftest.py`'s `app_server` fixture flips it on in the throwaway copy of
`app/` the suite runs from, where the dumps land in pytest's tmp dir and
are discarded with the session. A normal `python run.py` writes nothing —
deliberately, since these files hold whole file contents and whole
conversations and nothing prunes them. `qa_agent/llm_capture.py` reads
them back (filesystem only — still no `app.*` import), joining a capture
to a finding on the response text, which appears verbatim on both sides.

This paid for itself immediately. A `reply` finding judged "no" for
answering about "database queries" against a diff that only added a
`farewell()` function turned out to have been given *nothing but the bare
question* — messages was `[{"role": "user", "content": "Why was this
change made?"}]`, no diff and no history, because the test asked before
narration landed and the per-hunk history is empty until then. That's the
difference between "the model hallucinated" and knowing why. Note the app
permits the same race for a real reviewer (the composer is enabled before
narration arrives, by design), so a fast typist can get the same
context-free answer.

## Bugs this suite has already caught

Real, proven value from building it, not hypothetical:

- **`handle_act_now` (app/server.py)** was unpacking `_line_context()`'s
  return as a 3-tuple after it had been extended to 4 (adding `anchor`
  for the inline-comments feature) — Act Now was silently throwing on
  every call. `test_act_now.py` caught this on its first real run.
- **`websocket_endpoint` (app/server.py)** called `get_review_hunks()` —
  a `git diff` subprocess — directly on the event loop instead of via
  `asyncio.to_thread` (unlike the identical call in `handle_refresh_diff`,
  which was already wrapped correctly). Invisible with one human tester
  in one tab; this suite's rapid reconnects turned one slow/contended git
  call into a total server freeze for every client. Fixed to match the
  already-correct pattern elsewhere in the same file.
- **This suite's own `app_server` fixture** had a classic subprocess-pipe
  deadlock: `subprocess.PIPE` was never drained during normal operation,
  so once uvicorn's per-request log lines filled the OS pipe buffer, the
  server's next log write blocked — freezing the whole process. Fixed
  with a background thread continuously draining the pipe.

## Not yet built (see the plan this was created from)

- Stage 3's *original* design — an LLM agent reading a natural-language
  scenario description and deciding its own sequence of clicks/typing via
  Playwright, judged afterward — is still deferred, and still flagged as
  the highest-risk piece of the original 5-stage plan (a small local
  model has to reliably output structured actions turn after turn, not
  just a one-shot verdict). What was actually built under the "Stage 3"
  name instead (`qa_agent/scenarios/`) is a fixed, hand-scripted set of
  realistic navigation/conversation scenarios — see "What's covered"
  above for why that diverged from this original design.
- Stage 5 — driving the same inline-comment/Finish-Review flow this
  suite already exercises (Stage 1's `test_inline_comments.py`) to
  *author* a bug report from Stage 4's findings, for a human to hand to
  a fresh Claude Code session.
