# AI Pear Review for VS Code

> **Early development (0.0.x).** The review loop works end to end: the
> Changes tree, the native diff, the chat with voice, Look deeper, inline
> comments, Create Plan and Act Now, plus questions about any file and
> markdown read aloud. Packaging comes next.

A VS Code front end for [AI Pear Review](https://github.com/PearsReview/ai-pear-review).
It walks you through your uncommitted git changes one hunk at a time, with an AI
reviewer you can talk to. The Python backend is the same one the web app uses,
included here as a git submodule (`backend/`). The web app is unaffected and
still works on its own.

## Requirements

- VS Code 1.106 or later, with a **local** workspace. Remote, WSL and Codespaces
  windows aren't supported.
- Python 3.10+ with the backend's requirements and `sounddevice` for the mic.
- The model and speech services the backend is configured for (by default,
  Ollama, plus a local STT/TTS service on port 8000). See the backend's
  [README](backend/README.md).

## Using it

1. Open a git repository that has uncommitted changes.
2. Open the **Pear Review** view from the activity bar. It opens your changes
   straight away: browse the diffs and ask about them in the chat, which opens
   as a **Pear Review** tab in the secondary side bar, beside other chat
   extensions such as Claude Code. Press
   **Start Review** when you want the reviewer to explain each change as you
   go, and to mark changes reviewed and leave comments.
3. The **Changes** tree lists every changed file and its hunks. The current
   hunk opens as a diff (HEAD ↔ working file) with its lines highlighted.
   Click any hunk to jump to it; use the ↑/↓ buttons on the tree for
   Prev/Next, and ✓ on the current hunk to mark it reviewed.
4. The reviewer explains each change in the **Chat** view. Reply by typing,
   or press **Ctrl+Alt+Space** (**Cmd+Alt+Space** on macOS) to start
   recording and again to send. VS Code has no key-release event, so
   push-to-talk is press-to-start, press-to-stop.
5. To ask about particular lines, select them in the diff (either side)
   before you ask. The chat shows "Asking about calc.py, lines 2–4"; the
   selection goes with that one question.
6. **Look deeper** under a reply has your coding agent (Cline) read the
   repository for a more thorough, read-only answer. 🔊 reads a reply aloud,
   and **Interrupt** stops whatever the reviewer is doing.
7. To leave a comment for your coding agent, hover the diff's gutter and press
   **+** (drag over several lines first to comment on all of them), then type
   what should change and press **Must fix**, **Suggestion** or **Nit**. Or
   press the **mic** in the comment box's title bar, say it, and press it again:
   your words become the comment. Comments can be edited, re-tagged or deleted
   until you create the plan.
8. **Act Now**: press **Act Now** next to the message box, then type or say
   what to change (selected lines go with it). Your coding agent works in a
   copy of the repo and proposes the change; each file opens as a diff.
   **Apply** writes it, **Refine** asks for changes to the proposal, and
   **Discard** drops it. Nothing touches your files until you apply.
9. **Ask Pear About This File** (right-click a file in the Explorer, or in
   the editor) points the chat at any file in the repo, changed or not, even
   before a review starts. Ask by text or voice, with selected lines as
   context. **Back to review**, or moving to another change, returns the
   chat to the review.
10. **Read Aloud** on a markdown file (the speaker button in the title bar of
    the file or its preview, or right-click it) reads it through the chat,
    highlighting the passage being read. Select lines first to read only those.
    VS Code's panels can't play sound until they've been clicked once, so the
    first time, press ▶ on the chat's "Reading" line. (To preview a markdown
    file, press Ctrl+Shift+V, or Ctrl+K V for a side-by-side preview; a
    changed markdown file also has preview and read-aloud buttons on its row in
    the Changes view.)
11. **Create Plan** (the checklist button on the Changes view) writes all
    comments to `.review/review_<time>.md`, optionally as an `/apply-review`
    skill too, and gives you the line to hand your coding agent.
12. When every change is marked reviewed (one at a time, or all at once with
    ✓✓ on the Changes view), or you choose **End Review**, the chat shows a
    summary: how much was reviewed, the comments waiting, and buttons to
    create the plan, open the last plan, or start a new review.

While a reply is read aloud, the sentence being spoken is highlighted in it.
The filter in the chat's toolbar shows only the conversation about the current
file. If a change is too large for the model, the chat says so and offers the
request to copy into your coding agent. The status bar's **Pear** item names
any service that's down; hover it for the model, speech, coding agent and the
session's token use.

**Settings**: the ⚙ in the chat is the one place for settings. It opens the same
settings as the web app's panel:

- **Preferences**: explain changes automatically or only when you ask, speak
  replies aloud, and voice input on or off. Kept by the extension.
- **Reviewer model**: provider and model, context size, reply length and
  timeout, and the Anthropic API key.
- **Speech**: the text-to-speech and speech-to-text service endpoints and
  tokens.
- **Coding agent**: Cline, or none. Act Now and Look deeper need one. The agent
  uses the model you set up in Cline itself (`cline auth`), which may be a paid
  API.
- **Review context**: whether the prep files (project overview, call map,
  change briefings) are present and up to date, and how to refresh them.

Model, speech and agent settings are saved per repository, in the same place
the web app keeps them.

To use the Anthropic API instead of a local model, set the key under
**Reviewer model** in settings (⚙), then choose Anthropic as the model. The key
is kept in VS Code's secret storage and passed to the backend on its next
start.

## Developing

```
git clone --recurse-submodules <this repo>
npm install
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt "sounddevice>=0.4,<1.0"
```

Press **F5** to launch an Extension Development Host. The extension uses this
repo's `.venv` when there is one. Otherwise it uses the `pearReview.pythonPath`
setting, then `python` on PATH.

```
npm run lint && npm run typecheck && npm test
```

### Tests

Three layers, replacing the web app's Playwright suite for this front end:

| Command                           | What runs                                                                                                                         | Time    |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `npm test`                        | Unit tests of the pure helpers, and DOM tests of the chat panel's script under jsdom (clicks, rendering, audio states)            | seconds |
| `npm run test:integration`        | A real VS Code (downloaded once into `.vscode-test/`) with the extension, opened on a scratch repo, with a real backend behind it | ~15 s   |
| `npm run test:integration:ollama` | The same suite with your real local Ollama as the reviewer model                                                                  | minutes |

In the integration suite everything the backend calls out to is a fake
(`test/integration/`): a local server stands in for Ollama and the speech
service and records every request, the backend's scripted agent
(`tests/fake_acp_agent.py`) stands in for Cline, and a fake `sounddevice`
records a tone instead of opening the microphone. The backend runs from a
temporary copy with a patched config, so `backend/` is never edited. Only the
model switches to the real one with `:ollama`.

Tests drive the extension through its commands and the chat's message handler,
and read UI state through a test probe the extension exposes only when
`PEAR_REVIEW_TEST=1` (`src/testProbe.ts`).

How the code is laid out, and the rules it follows, are in
[docs/STYLE.md](docs/STYLE.md).

## Credits

The chat panel's icons are VS Code's [codicons](https://github.com/microsoft/vscode-codicons),
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
