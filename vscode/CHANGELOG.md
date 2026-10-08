# Changelog

## Unreleased

- **Review Pull Request…** reviews a GitHub pull request: it's fetched into a
  separate worktree (your checkout is untouched) and diffed against its
  merge-base, as GitHub's Files changed shows it. The review is read-only (no
  Act Now, no plan), and **Submit Review** posts the comments to the PR as one
  review (Comment, Request changes or Approve). Comments outside GitHub's diff
  go on the file. Signs in with VS Code's GitHub account.
- Pull request reviews work with GitHub Enterprise (Server, or Cloud on
  `ghe.com`): set VS Code's `github-enterprise.uri` and that host's remotes are
  listed, signing in with VS Code's GitHub Enterprise account.
- Backend: `REVIEW_BASE_SHA` diffs against a commit instead of HEAD (no
  untracked files), and `REVIEW_READ_ONLY=1` refuses every handler that writes.
- **Set OpenAI-compatible API Key** and an **OpenAI-compatible** provider in
  the settings panel: it asks for the base URL, then lists the endpoint's
  models. The key is kept in VS Code's secret storage and passed to the
  backend as `OPENAI_API_KEY`.
- **Open Chat** button on the Changes view's title bar.
- Python: the managed venv from **Set Up Python Environment** is now used in
  preference to `pearReview.pythonPath`; that setting is used to create the
  venv, and as the backend's interpreter only when no venv exists. Setup also
  looks for `python3.13` … `python3.10` on PATH (Homebrew, pyenv). Existing
  users should run **Set Up Python Environment** again to install the
  `openai` package (only needed for the OpenAI-compatible provider).
- Internal: `errorMessage()` / `showError(unknown)` and a shared
  `nextMessage()` replace code repeated across the backend and UI modules.

## 0.0.2

- The extension moved into the backend's repo (`vscode/`), replacing the
  `backend/` submodule. The `.vsix` still carries a copy of the backend, now
  made by `npm run sync-backend` (run by `package` and `test:package`).
- Internal: Read Aloud, its highlight, and asking about a file are separate
  modules (`readAloud.ts`, `readingHighlight.ts`, `repoFiles.ts`); the chat's
  audio player is its own script (`media/chat/audio.js`); and the webview's
  scripts are type-checked by `tsconfig.webview.json`, as part of
  `npm run typecheck`.
- Docs: STYLE.md covers the backend manager, why the extension runs locally
  (`extensionKind: ui`), where the Python comes from, and the three integration
  workspaces; the README covers setting up Python, several repositories, Get
  Started, suggested questions, explain-when-asked and Read Aloud's limits.

- Suggested questions above the message box, as in the web app: for the change
  on screen, for selected lines, for a file asked about, and instructions in
  Act Now mode. A chip fills the box to edit or send.
- **Explain changes** defaults to **When I ask**, as in the web app: moving to
  a change doesn't call the model until you press ✨. Choose Automatically in
  the chat's ⚙ to have every change explained.
- **Pear Review: Get Started** opens a walkthrough (also in the chat's ⚙):
  set up Python, open the changes, start the review, ask, comment and plan,
  Act Now. It replaces the web app's guided tour.
- Before the review starts, the chat's first line says how to begin.

- **Pear Review: Set Up Python Environment** makes a Python environment for the
  extension (in its global storage) from a Python 3.10+ it finds (the Python
  extension's choice first) and installs the backend's packages and
  sounddevice. The interpreter is now the `pearReview.pythonPath` setting,
  then that environment (or, when developing, the extension's `.venv`);
  `python` on PATH is no longer guessed. A missing environment, or one without
  the backend's packages, says so with a **Set Up Python** button.
- Packaging: `npm run package` builds the `.vsix`, and `npm run test:package`
  checks it holds what the backend needs. Fixed: the package left out
  `backend/static`, which the backend mounts on start, so an installed
  extension's backend couldn't start.
- The extension declares that it needs a trusted workspace.

- Several repositories in one window: each gets its own backend and its own
  review. **Switch Repository** (on the Changes view when there's more than
  one) or Ask Pear / Read Aloud on a file in another repository changes which
  one is shown; the others keep running, and switching back resumes that review
  where it was. The Changes view names the repository shown. Closing a
  repository's folder stops its backend. With several repositories, nothing
  opens on its own unless the active editor's file is in one.
- A folder that isn't a git repository gets a welcome that says so, with Open
  Folder, and no backend starts.

- Act Now's proposal is locked while the agent refines it, or while it's being
  applied: Apply, Refine and Discard (in the notification and the diff's title
  bar) wait for the answer, so the old proposal can't be applied mid-refine.
- Fixed: pressing Enter to pick an IME candidate (Chinese, Japanese, Korean
  input) sent the half-typed message.
- Setting the Anthropic API key offers to restart the backend, which is when it
  takes effect.
- Read Aloud plays 8-, 24- and 32-bit and floating-point WAV speech, not only
  16-bit.
- Recognising an applied Act Now change, and the start and end of a file read,
  no longer depends on the wording of the backend's notices.

- Safer and sturdier Read Aloud. The backend no longer loads the reviewed
  repo's `.env` (its variables reached the coding agent). Reading no longer
  moves the cursor or selection. A read cancelled on the backend by another
  action (a question, Next…) ends instead of waiting forever. The chat's audio
  and a file read never play at once: whichever starts takes over. A player
  that can't start reports an error instead of failing silently in the
  extension host. Text-to-speech that isn't WAV gets a clear message.
- Reading a preview's selection is opt-in (`pearReview.readPreviewSelection`),
  because it goes through the clipboard. The preview's file is found from its
  tab in any display language, and two files with the same name are never
  confused: Read Aloud asks for the text instead.

- Read Aloud from a preview reads the text selected in the preview, as a
  selection in the file's text does; with nothing selected it reads the whole
  file.
- The passage being read aloud is highlighted, and scrolled into view, in
  VS Code's markdown preview itself (a markdown-it plugin plus a preview style
  and script); reading from a preview no longer opens the text beside it.
- Read Aloud plays through the extension (a small player using sounddevice), so
  it no longer waits for a click in the chat. It's controlled where it's
  started: the speaker on the file's row, editor or preview title bar, or
  Explorer menu becomes pause, play and stop. The highlight follows the
  player's position. The chat's "Reading" line is gone.
- The chat holds the conversation only: Act Now proposals, the review's
  summary, notices, errors and agent stops are VS Code notifications or status
  bar messages, with their buttons.
- Fixed: the Changes view and the chat appearing together could start two
  backends.
- Read Aloud from a markdown preview opens the file's text beside it, with the
  passage being read highlighted and the cursor on it, so the preview scrolls
  along. Fixed: Read Aloud from a preview whose text wasn't open did nothing.
- Changed markdown files have Open Preview and Read Aloud on their row in the
  Changes view (inline and in the right-click menu).
- Read Aloud on a markdown preview's title bar too. Audio the chat may not play
  yet (no click in the panel so far) now says so beside its ▶, and the log
  records it.
- The web app's extras: the read-along highlight of the sentence being spoken
  (and, reading a file, of the block in the editor); a filter for the current
  file's conversation; a hand-off card when a change is too large for the
  model; service status and token use on the status bar.
- The review's summary in the chat when it ends (reviewed count, comments
  waiting, Create plan, Open plan, Start new review); Mark All Reviewed (✓✓)
  on the Changes view; Show Summary and Start New Review in its menu.
- Spoken review comments: a mic in the comment box's title bar records the
  comment, which is added as a Suggestion on the box's lines.
- Settings (⚙ in the chat, the one way in, holding the model, coding agent
  and API key choices too): the web app's settings panel as a native menu — explain automatically or on request, speak replies,
  voice input, reviewer model and limits, Anthropic key, speech services,
  coding agent, and the prep files' freshness.
- The chat is a Pear Review tab in the secondary side bar, beside other chat
  extensions; it comes forward the first time the changes open. Needs VS Code
  1.106 or later.
- Opening the Pear Review view opens your changes: browse the diffs and chat
  without pressing Start Review, which now just turns on narration, reviewed
  marks and comments (Open Changes in the Command Palette does the same).
- Audio is controlled from each message's own speaker button, with no
  separate audio bar: a spinner while speech is made (click to cancel), pause
  while it plays, play while paused. Narration plays under its own message; a
  markdown file read aloud gets a "Reading <file>" line with the same button.
- Fixed: finishing a read aloud marked an open Act Now proposal as applied.
- Prev/Next and Start Review (▶, until the review starts) only on the Changes
  view's title bar; the chat toolbar keeps Explain.
- Restyled the chat panel: VS Code's codicons in place of emoji and text
  buttons, grey icon buttons and outlined pills, a rounded composer, slim strips
  for audio, file and selection, and colour only for recording and applied
  proposals.
- Tests replacing the Playwright suite for this front end: DOM tests of the
  chat panel (jsdom) and an integration suite in a real VS Code against a real
  backend with fake model, speech, agent and microphone (29 tests, ~15 s), with
  an opt-in run against real Ollama.
- Fixed: a comment on several lines lost its last line.
- Fixed: Start Review right after VS Code opened could report "Open a git
  repository" while git was still finding it.
- Ask Pear About This File: the chat answers questions about any repo file
  (text or voice, selection as context), from the Explorer or editor menus.
- Read Aloud for markdown files, with the passage being read highlighted;
  also offered for a freshly created plan.
- Act Now: act mode in the chat (typed or voice, with selected lines), the
  proposal as read-only diffs with Apply / Refine / Discard in the chat card
  and the diff's title bar. Choose Coding Agent and Choose Reviewer Model,
  saved per repo like the web app's settings panel.
- Review comments as native comment threads (Must fix / Suggestion / Nit),
  editable until Create Plan, on either side of the diff; Create Plan writes
  the hand-off document and optional /apply-review skill. End Review.
- Chat: markdown replies, hunk dividers you can click back to, related-change
  links, Look deeper with an elapsed timer, read-aloud per reply, Interrupt,
  and the editor selection (either diff side) sent as a question's context.
- Changes tree (files → hunks, reviewed state), native HEAD ↔ working-file
  diff with the current hunk highlighted, Next/Prev/jump, Mark reviewed and
  Refresh. New and deleted files open against an empty side.
- Voice spike: start the backend from VS Code, have the reviewer explain the
  current change, reply by text or by voice (Ctrl+Alt+Space to start and
  stop recording), and hear the answer.
