# Style and coding guide

How code in this repo should read, and the shape it grows along.

**House style is inherited, not restated.** Part 1 of the backend's
[docs/STYLE.md](../backend/docs/STYLE.md) applies here unchanged: the comment
calibration rule (keep measurements, observed failures, rejected alternatives
and cross-module contracts; cut restatements and changelog), "one fact, one
home", and naming and size. This file adds only what is specific to a
TypeScript VS Code extension.

## 1. Tooling

- TypeScript with `strict`, `noUncheckedIndexedAccess`, `noImplicitOverride`
  ([tsconfig.json](../tsconfig.json)). `tsc` only type-checks; esbuild bundles.
- ESLint with `typescript-eslint`'s type-checked recommended set, and Prettier
  at **120 columns** — the backend's ruff `line-length`, so both halves wrap alike.
- No `any`. Input from outside the process (the socket, the webview) arrives as
  `unknown` and is narrowed by a guard (`isServerMessage`, `parseFromWebview`).
- The webview's plain scripts (`media/`) are type-checked too, by
  [tsconfig.webview.json](../tsconfig.webview.json) against
  `media/chat/globals.d.ts`, which declares each script's one exported global.
- `npm run lint`, `npm run typecheck` and `npm test` all run in CI and must pass.

## 2. Layering

```
src/ui/*  →  Backend interface  →  BackendManager (src/backend/manager.ts)
                                    →  PythonBackend per repository (src/backend/backend.ts)
                                    →  WsClient / BackendProcess
```

- `BackendManager` implements `Backend` and keeps one `PythonBackend` per
  repository. The UI sees only the repository it shows: its messages, its state,
  its sends. Switching passes through `starting`, and every UI module resets on a
  state that isn't `ready`, then rebuilds from what the new connection sends. A UI
  module never needs to know there are several.

- UI modules never touch the socket or the child process. Everything goes through
  `Backend.send` / `Backend.on`. That interface is the seam any piece of Python
  can later be replaced behind.
- `src/backend/` never imports `src/ui/`.
- Every wire message is a typed entry in
  [src/backend/protocol.ts](../src/backend/protocol.ts). Its source of truth is the
  backend's [wire-protocol.md](../backend/docs/wire-protocol.md);
  `test/unit/protocol.test.ts` fails if the two disagree in either direction.
  A message no UI reads yet is typed `Unknown` until one does.

## 3. Module shape

- One file per UI surface or job (`chatPanel`, `hunkTree`, `diffView`, `comments`,
  `readAloud`, `repositories`, …). A module that grows a second job is split.
- Each exports `register(...)`, which returns its disposables, plus a small API
  object when another surface needs one (`voice` does).
- [src/extension.ts](../src/extension.ts) only wires surfaces together. It holds no logic.

## 4. VS Code rules

- Every disposable ends up in `context.subscriptions`.
- `activate()` never blocks and never spawns a process. A repository's backend
  starts when the Pear Review view or chat is first shown (when there's exactly
  one repository, or the active editor's file is in one), on **Start Review**, or
  on a command that needs it (Ask Pear, Read Aloud, settings).
- Commands, settings, views and context keys are all `pearReview.*`.
- User-facing failures go through `showError` in [src/log.ts](../src/log.ts). It
  logs to the "Pear Review" output channel and offers **Show Log**, so nothing
  fails silently or only in a console.
- Remote workspaces are unsupported: `extensionKind` is `["ui"]`, and Start Review
  refuses in a remote window. The extension records from the microphone and plays
  speech on the reviewer's own machine (`sounddevice`, in the backend and in
  `python/player.py`), so it has to run beside them; in a remote window that is
  the wrong machine.
- Untrusted workspaces are unsupported (`capabilities.untrustedWorkspaces`): the
  backend and the coding agent read and write the repository.
- The Python the extension runs is chosen in one place, `resolvePython` in
  [src/backend/python.ts](../src/backend/python.ts): the `pearReview.pythonPath`
  setting, then the environment **Set Up Python Environment** made. A missing one
  is an error that names the set-up command, and `showError` offers it.

## 5. Webviews

- A strict CSP with a fresh nonce per render. No inline scripts or styles.
  `localResourceRoots` is limited to the view's own `media/` folder.
- Messages in both directions are a discriminated union (`kind`), declared next to
  the provider (`ToWebview` / `FromWebview`). The webview script handles exactly
  those kinds.
- The webview talks only to the extension, never to the backend directly.
- Style with VS Code's theme variables (`--vscode-*`), never fixed colours.

## 6. Async and cancellation

- Long-running work (narration, Look deeper, Act Now, recording) is cancelled the
  backend's way, with its `stop` message and `cancels` semantics. Don't invent a
  second cancellation scheme in TypeScript.
- No floating promises. `await` them, or mark them `void` when ignoring the
  result is the point (`no-floating-promises` enforces it).

## 7. Backend changes

The backend is a git submodule and is **read-only from this repo**. A change it
needs is made in the AI_Pear_Reviewer repo on its own branch, following that
repo's STYLE.md, ruff, mypy and its §3.4 "adding a message type" checklist (which
includes the wire-protocol.md row). It must leave the browser UI working. Once
merged, bump the submodule pin here.

## 8. Tests and definition of done

- Pure logic goes in `test/unit/` (`node --test`, no VS Code).
- The chat webview's script is tested in a DOM (`test/unit/chat.dom.test.ts`,
  jsdom): its markup comes from `src/ui/chatHtml.ts`, so the tests render exactly
  what the panel serves.
- The extension's own Python (`python/`) is tested in `test/python/` (pytest).
- Behaviour that needs VS Code goes in `test/integration/suite/` (`@vscode/test-electron`),
  against a real backend with fakes for the model, speech, agent and microphone. Tests
  act the way a user does (commands, the chat's message handler) and read state through
  the test probe (`src/testProbe.ts`). A module that needs checking publishes a
  read-only view there; it never gains test-only behaviour.
- The integration suite runs three workspaces, each its own VS Code instance
  (`test/integration/runTest.ts`): the main repository (the numbered files), a
  multi-root workspace with two repositories (`multi_*`), and a folder that isn't a
  repository (`nogit_*`).
- Every message type the UI sends has a test path. A new feature adds an integration
  test, and a DOM test if it changes the chat panel.
- A change is done when:
  - [ ] lint, typecheck, `npm test` and `npm run test:integration` pass;
  - [ ] the README and CHANGELOG are updated if behaviour changed;
  - [ ] the manual F5 walkthrough still works (README, "Developing").
