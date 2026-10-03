# Changelog

## Unreleased

- Opening the Pear Review view opens your changes: browse the diffs and chat
  without pressing Start Review, which now just turns on narration, reviewed
  marks and comments (Open Changes in the Command Palette does the same).
- One audio bar: speech the panel isn't yet allowed to play waits on its play
  button, instead of a second "Spoken replies are waiting" strip.
- Prev/Next only on the Changes tree, not the chat toolbar.
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
