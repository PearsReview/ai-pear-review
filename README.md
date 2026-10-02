# AI Pear Review for VS Code

> **Early development (0.0.x).** Working so far: the Changes tree, the
> native diff with the current change highlighted, Next/Prev, marking changes
> reviewed, and the chat: explanations, replies by text or voice, Look
> deeper, and selected lines as context. Inline comments, the review plan and
> Act Now come next.

A VS Code front end for [AI Pear Review](https://github.com/PearsReview/ai-pear-review).
It walks you through your uncommitted git changes one hunk at a time, with an AI
reviewer you can talk to. The Python backend is the same one the web app uses,
included here as a git submodule (`backend/`). The web app is unaffected and
still works on its own.

## Requirements

- VS Code 1.95 or later, with a **local** workspace. Remote, WSL and Codespaces
  windows aren't supported.
- Python 3.10+ with the backend's requirements and `sounddevice` for the mic.
- The model and speech services the backend is configured for (by default,
  Ollama, plus a local STT/TTS service on port 8000). See the backend's
  [README](backend/README.md).

## Using it

1. Open a git repository that has uncommitted changes.
2. Open the **Pear Review** view from the activity bar and press **Start
   Review**, or run **Pear Review: Start Review** from the Command Palette.
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

To use the Anthropic API instead of a local model, run **Pear Review: Set
Anthropic API Key**. The key is kept in VS Code's secret storage and passed
to the backend on its next start.

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

How the code is laid out, and the rules it follows, are in
[docs/STYLE.md](docs/STYLE.md).
