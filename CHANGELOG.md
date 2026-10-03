# Changelog

## Unreleased

- The web app's extras: the read-along highlight of the sentence being spoken
  (and, reading a file, of the block in the editor); a filter for the current
  file's conversation; a hand-off card when a change is too large for the
  model; service status and token use on the status bar.
- The review's summary in the chat when it ends (reviewed count, comments
  waiting, Create plan, Open plan, Start new review); Mark All Reviewed (✓✓)
  on the Changes view; Show Summary and Start New Review in its menu.
- Comment mode in the chat: the next typed or spoken message becomes a review
  comment on the selected lines or the current change, confirmed in the chat
  with its wording.
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
