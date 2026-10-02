# Changelog

## Unreleased

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
