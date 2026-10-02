# AI Pear Review for VS Code

> **Early development (0.0.x).** This is the voice spike: start a review, hear
> the reviewer explain the current change, and answer by voice or text. The
> hunk tree, diff view, comments, plan and Act Now come next.

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
2. Run **Pear Review: Start Review** from the Command Palette, or open the
   Pear Review view and press **Start review**.
3. The reviewer explains the current change.
4. Reply by typing, or press **Ctrl+Alt+Space** (**Cmd+Alt+Space** on macOS)
   to start recording and press it again to send. VS Code has no key-release
   event, so push-to-talk is press-to-start, press-to-stop.
5. Use **Next** and **Prev** to move between changes.

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
