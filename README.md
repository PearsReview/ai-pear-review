# AI Pear Review — AI code review in VS Code, one hunk at a time

[![CI](https://github.com/PearsReview/ai-pear-review/actions/workflows/ci.yml/badge.svg)](https://github.com/PearsReview/ai-pear-review/actions/workflows/ci.yml)

> **Beta (0.0.x).** It works, but expect rough edges, and settings or
> file formats may change between releases — see
> [Known issues](#known-issues-and-limitations).

A VS Code extension for AI code review: it walks you through your git diff or
a GitHub pull request one hunk at a time, with an AI reviewer you can talk to.
It explains each change, answers questions about it by text or voice, and
(optionally) has your coding agent make small edits you agree on. It works
with a local LLM through Ollama or with hosted models such as Claude and
OpenAI-compatible APIs. Everything runs against your own checkout. There is
also a [standalone web app](#web-app) on the same backend.

Three things it's for:

- **Your own changes, before you commit.** That includes a batch a coding
  agent just made for you, which you want to understand line by line rather
  than accept on trust.
- **GitHub pull requests.** Check one out with GitHub's Pull Requests
  extension, and Pear explains its changes and answers questions about them.
- **Reading Markdown aloud.** Any `.md` file can be read to you, such as a
  plan, a design doc or an agent's hand-off notes, with the passage being read
  highlighted as it goes.

> A **hunk** is one contiguous block of changed lines in `git diff`. A
> file with three separate edits has three hunks; each one is a step of
> the review.

![The Changes tree, the native diff of a change, and the reviewer's explanation of it in the chat.](vscode/media/screenshot.png)

## What it does

- The **Changes** view lists every changed file and its hunks. Each hunk
  opens as VS Code's own diff, with its lines highlighted.
- Press ✨ and the reviewer explains the change on screen. You can also set it
  to explain every change as you reach it. Ask by typing, by voice
  (**Ctrl+Alt+Space**), or with lines selected in the diff as context.
- Leave comments in the diff's gutter, tagged **Must fix**, **Suggestion** or
  **Nit**. **Create Plan** bundles them into one hand-off for your coding
  agent.
- **Act Now** has your coding agent propose a small change as a diff, which
  you apply, refine or discard.
- **Look deeper** hands a question the reviewer can't answer to a coding
  agent, which investigates the repo read-only.
- **Ask Pear About This File** works on any file in the repo, not just the
  changed ones.
- **Review Pull Request…** reviews a GitHub pull request; see
  [below](#reviewing-a-pull-request).
- **Read Aloud** reads any Markdown file to you; see
  [below](#reading-markdown-aloud).
- Review progress and queued comments are saved, so a reload or restart
  doesn't lose them. Ending a review locks marks and comments, but the chat
  keeps working, and **Reopen Review** picks the review back up.

**Pear Review: Get Started** in VS Code walks through all of this.

## Recommended setups

Each part runs locally or on a hosted service. Only the reviewer model is
required; anything missing disables its own feature and nothing else.

| Part | Everything local (a capable GPU) | Hosted models (company API access) |
|---|---|---|
| Reviewer model (explain + chat) | Ollama (the default) | Anthropic API, or any OpenAI-compatible endpoint/gateway |
| Act Now + Look deeper | Cline, set up with your local model | Cline, set up with Anthropic |
| Voice in and out, Read Aloud | [stt_tts](https://github.com/PearsReview/stt_tts) (CPU is enough) | Your company's STT/TTS endpoint |
| Prep skills | Cline on the local model, or Claude Code | Claude Code or Cline |

Mix freely — e.g. local narration with a hosted model behind Cline. Local
models are noticeably weaker at Act Now and Look deeper than at narrating.

**On cost:** Pear never runs the `claude` CLI, so it never spends a Claude
Code subscription. The Anthropic provider is the per-token API, and the
OpenAI-compatible provider the per-token cost of whatever endpoint you point
it at. Act Now and Look deeper use whatever model and credentials you gave
Cline.

## Requirements

- VS Code 1.106 or later, with a **local**, trusted workspace (not Remote, WSL
  or Codespaces: the mic and speaker are on your machine)
- Python 3.10+ and `git` on PATH; [Node.js](https://nodejs.org) 20+ to build
  the extension
- A git repo with at least one commit
- A model: a local [Ollama](https://ollama.com) server (the default), an
  Anthropic API key, or any OpenAI-compatible endpoint
- Optional: [Cline](https://docs.cline.bot) for Act Now and Look deeper, an
  STT/TTS endpoint (e.g. [stt_tts](https://github.com/PearsReview/stt_tts))
  for voice and Read Aloud, GitHub's
  [GitHub Pull Requests](https://marketplace.visualstudio.com/items?itemName=GitHub.vscode-pull-request-github)
  extension for pull request reviews, and
  [Claude Code](https://claude.com/claude-code) or Cline for the prep skills

## Install

Steps 1–3 get you a running review; 4 and 5 add optional features.

**1. Build and install the extension.** It isn't on the Marketplace yet:

```
git clone https://github.com/PearsReview/ai-pear-review.git
cd ai-pear-review/vscode
npm install
npm run package
code --install-extension ai-pear-review-<version>.vsix
```

(Or **Install from VSIX…** in the Extensions view's **⋯** menu.) The `.vsix`
carries its own copy of the backend.

**2. Set up Python.** Reload VS Code when it offers to, then run **Pear
Review: Set Up Python Environment**. It makes a private environment with
everything the backend needs. Platform notes are in
[vscode/README.md](vscode/README.md#installing).

**3. Set up a model.** For the default, install and start
[Ollama](https://ollama.com). It must be *running*, not just installed: the
desktop app starts it, as does `ollama serve`. Then pull the model (about
5 GB):

```
ollama pull qwen2.5-coder:7b-instruct-q4_K_M
```

> **Using a hosted model instead?** Skip Ollama. In the chat's ⚙, under
> **Reviewer model**, choose **Anthropic** or **OpenAI-compatible** (for any
> gateway such as LiteLLM, vLLM or an enterprise proxy, also set its base URL
> and model), and set the API key there. The key is kept in VS Code's secret
> storage.

Then open a repo with changes, open **Pear Review** from the activity bar,
and you're reviewing. The working files (`.review/`, `.briefing/`,
`.context/`) are kept out of your `git status` automatically.

**4. (Optional) Set up Cline** for Act Now and Look deeper:

```
npm i -g cline
cline auth
```

`cline auth` is where you choose Cline's provider and model. Then pick Cline
under **Coding agent** in the chat's ⚙. Don't run Cline with auto-approve.

**5. (Optional) Voice and Read Aloud.** For an all-local setup, run
[stt_tts](https://github.com/PearsReview/stt_tts), a small speech service
(faster-whisper + Kokoro, CPU-only). It serves exactly what the defaults
expect at `http://localhost:8000`, so there's nothing to configure. To use
another speech-to-text / text-to-speech service instead, set its endpoints
under **Speech** in the chat's ⚙; any service matching
[the expected request/response shape](docs/configuration.md) works.

### If something's wrong, it says so

The backend checks your setup as it starts, and the **Pear** item in the
status bar names any service that's down. The full report is in the **Pear
Review** output channel (or the terminal, for the web app):

```
AI Pear Review — checking your setup

  [OK  ] git: found at /usr/bin/git
  [OK  ] repository: /home/you/your-project
  [OK  ] changes: 4 file(s) with uncommitted changes
  [WARN] model: qwen2.5-coder:7b-instruct-q4_K_M is not installed
         -> Run: ollama pull qwen2.5-coder:7b-instruct-q4_K_M
```

`FAIL` stops startup (no git, or not a git repo). `WARN` starts anyway, and
says what to fix.

## Prep skills (optional, recommended)

The reviewer model sees one hunk and nothing else, so when it doesn't know
something it tends to guess. Two skills, run in your own Claude Code or
Cline session *before* starting the review, give it real context. Pear only
reads what they leave behind.

| Skill | Answers | Rerun when |
|---|---|---|
| **project-overview** | What is this project, and what is this file for? | the architecture changes |
| **prep-review** | Why was *this hunk* written this way? | the diff changes |

(A third, **call-map**, is switched off for now: it only covered Python, and
"who calls this?" is to come from the coding agent instead. The installer
no longer copies it. See [Prep skills](docs/prep-skills.md).)

Install them into the repo you're reviewing, once per repo:

```
python /path/to/ai-pear-review/install_skills.py --repo ~/your-project
```

(add `--with-reminders` for a nudge to brief your changes before a session
ends, or `--with-auto-brief` to have Claude Code brief the changes it made
before it stops — see [Prep skills](docs/prep-skills.md#briefing-automatically-in-claude-code)). The installer keeps them out of the review automatically; commit the
installed `.claude/` files only if you want to share them with your team.

Then, in Claude Code or Cline opened in that repo, run `/project-overview`
and `/prep-review` in that order — or just ask "prep the review". Run
prep-review at the end of the session that made the changes, while it still
knows *why* each change was made. Later reviews in the same repo only need
`/prep-review`. **Review context** in the chat's ⚙ shows how old the
overview and change themes are, and when a review opens Pear says how many
changes have no up-to-date briefing.

For several repos at once, `--repos-file repos.txt` installs into every
path listed in a file (one per line), and `--check-repos repos.txt` reports
which of them have missing or out-of-date skills without writing anything.

More in [docs/prep-skills.md](docs/prep-skills.md): the opt-in
`--with-reminders` hook and Cline rule, and running the skills against a
repo without installing them.

## Reviewing a pull request

Pull requests are reviewed with GitHub's own **GitHub Pull Requests**
extension, and Pear adds its walkthrough, explanations and chat on top.
**Review Pull Request…** opens GitHub's Pull Requests view (or offers to
install the extension). Check the PR out there; **Checkout in Worktree**
leaves your own checkout and uncommitted changes alone. The Changes view then
lists the PR's changes against the point where it branched, as GitHub's
**Files changed** shows them, and you ask about them as in any review.
Comments, Viewed marks and the submitted review go to GitHub through GitHub's
extension. Pear's own comments, reviewed marks, Act Now and plans are off for
a pull request. GitHub Enterprise works too. The full steps are in
[vscode/README.md](vscode/README.md#reviewing-a-github-pull-request).

## Reading Markdown aloud

**Read Aloud** reads any Markdown file to you. It doesn't have to be a changed
file, and you don't have to start a review: a review plan Pear just wrote, a
design doc, a README or an agent's hand-off notes all work. Find it:

- as the speaker on a Markdown file's row in the Changes view,
- in a Markdown editor's or preview's title bar,
- on right-click in the Explorer.

While it reads, the same place shows pause, play and stop, and the preview
highlights and follows the passage being read. Select lines first to read
only those. It needs a text-to-speech service that returns WAV;
[stt_tts](https://github.com/PearsReview/stt_tts) works out of the box.

## Web app

The same review also runs as a standalone web app in your browser, for your
uncommitted changes. You don't need VS Code for it.

![The web app: the current hunk highlighted in a full-file diff on the left, and on the right the reviewer persona's narration of that hunk, a follow-up question, and its answer.](docs/img/review-ui.png)

<sub>Narrated by a local `qwen2.5-coder:7b-instruct-q4_K_M` through Ollama. There's a [dark theme](docs/img/review-ui-dark.png) too.</sub>

### Run it

```
git clone https://github.com/PearsReview/ai-pear-review.git
cd ai-pear-review
pip install -r requirements.txt

cd ~/your-project && python ~/ai-pear-review/run.py
python run.py --repo ~/your-project        # the same, from the app's folder
```

A virtualenv is recommended: `python -m venv .venv`, then
`.venv\Scripts\activate` (Windows) or `source .venv/bin/activate`.
`pip install -e .` instead of `-r requirements.txt` also puts two commands
on your PATH, `pear-review` (same as `python run.py`) and `pear-install`
(same as `python install_skills.py`), so you can run them from any repo
without naming the app's path.

It opens `http://127.0.0.1:8765` in your browser, or the next free port if
that one is taken. The model setup is the same as [step 3](#install) above;
for a hosted model, either pick it in the settings panel or set
`conversation.provider: anthropic` (or `openai`) in `app/config.yaml`, with
`ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) in a `.env` file at the app's root
(see `.env.example`) or in your shell. Every setting lives in the commented
`app/config.yaml`, and [docs/configuration.md](docs/configuration.md) walks
through it. To set defaults across every repo without editing that file, put
the keys you want to override in `~/.config/pear-review/config.yaml`. It's
layered on top of `app/config.yaml` at startup.

### Using the web UI

- **Start Review / End Review** — narration and chat begin only once you
  click Start Review; browsing the diff always works. Ending a review (End
  Review, or marking the last change reviewed) locks the reviewed marks,
  comments and Act Now, and says so; you can still open any change, ask about
  it and have it explained. **Reopen Review** picks the same review back up
  with its marks and comments; **Start New Review** starts over.
- **Prev / Next** — move between hunks.
- **Explain** (next to Next) — asks the AI to explain the change you're on.
  By default nothing is explained until you click it, so you choose which
  changes are worth a model call; you can also just ask a question without
  one. To have every change explained as you move to it, set **Explain
  changes** to *Automatically* in the chat's settings (the gear under the
  message box).
- **Mark as reviewed / Review all** — track progress; shown per file in the
  file explorer.
- **File explorer** (left) — jump to any changed file. The folder icon
  switches to **All files**, to open and ask about any file.
- **Merged / Split** — how the diff is drawn. **Refresh** re-reads the
  diff after you've changed files outside the app.
- **Chat** (right) — type, or push-to-talk with the mic. **Overall** shows
  every turn; **File** just this file's.
- **Line marking** — double-click a line to mark it as context for your
  next message; **+** on a line adds an inline review comment.
- **Create plan** (top toolbar, once comments are queued) — turns every
  queued comment into a plan for your coding agent, saved as
  `.review/review_<time>.md`. Optionally it's also saved as an agent skill,
  so in Claude Code or Cline you just run `/apply-review` (or ask it to
  apply the review); the dialog remembers your choice. The plan opens in the
  preview pane, with **Copy instruction** and **Copy plan** buttons, and
  **View review plan** (also on the end-of-review summary) reopens it. Each
  comment keeps the lines it's about and flags any whose code has changed
  since. **End Review** with comments still queued offers to create the
  plan first.
- **Act Now** — toggle on, describe a small change, and confirm the
  preview before anything is written. Needs Cline ([step 4](#install)).
- **Look deeper** — under each answer; the coding agent investigates the
  question read-only. Slower than a normal reply.
- **?** in the header replays the guided tour.

## Known issues and limitations

- **No branch or commit review yet.** VS Code reviews your uncommitted
  changes against `HEAD`, or a pull request against its merge-base. The web
  app reviews uncommitted changes only. Neither reviews a branch against its
  base, or past commits.
- **No model, no narration.** If Ollama isn't running or the API key is
  missing, the diff is still browsable but nothing narrates. The **Pear**
  status bar item (the **LLM** pill in the web app) shows this.
- **Slow model calls are dropped, not waited for**, so on slow hardware a
  hunk can simply never narrate — see
  [Performance and hardware](docs/configuration.md#performance-and-hardware).
- **Chat history doesn't survive a restart**; review progress and queued
  comments do.
- **Any language can be reviewed; few can be navigated.** Step Into only
  recognises Python and JavaScript, and nothing answers "who calls this?"
  while the call map is switched off.
- **Step Into is hidden for now** (web app). It only recognises Python and
  JavaScript. Remove `class="hidden"` from `#step-into-btn` in
  `static/index.html` to turn it back on.
- **One coding agent.** Act Now and Look deeper run through Cline only, and
  are only as good as the model you give it. Look deeper was tested mainly
  with Claude Sonnet 5; small local models give shallower answers.
- **Prep skills need Claude Code or Cline** — other assistants don't read
  `.claude/skills/`.
- **Lightly tested.** Only a few machines and models have been tried.

## TODO

- **Review beyond the working tree and pull requests**: a branch against its
  base, and past commits. Pull requests in the web app too.
- **Publish the extension** to the VS Code Marketplace.
- **Comments and Act Now on unchanged files**: asking about any file works,
  but not commenting on it or editing it.
- **Keep the conversation** across restarts, and let it be exported.
- **More languages** for Step Into (e.g. via tree-sitter), then bring the
  Step Into button back.
- **Callers and tests from the coding agent**, replacing the call map (see
  [vscode/TODO.md](vscode/TODO.md)).
- **More coding agents**: Act Now and Look deeper use the open Agent
  Client Protocol (ACP), so other ACP agents can be added alongside Cline.
- **Skills for other assistants**: Cursor rules, Copilot instructions,
  `AGENTS.md`.
- **Install from PyPI**: `pip install -e .` already gives the `pear-review`
  and `pear-install` commands and `~/.config/pear-review/config.yaml` is
  layered over `app/config.yaml`; what's left is packaging `static/` and the
  bundled skills so a plain `pipx install ai-pear-review` (no checkout) works.

## How it works

The extension and the web app are two front ends on one Python backend
(`app/`). The extension starts its own copy of it per repository. Narration
and chat are one model call per turn to Ollama, the Anthropic API or an
OpenAI-compatible endpoint, with no tools. Act Now and Look deeper are handed
to Cline over the [Agent Client Protocol](https://agentclientprotocol.com),
always in a temporary copy of your repo: Look deeper can't write at all, and
an Act Now edit reaches your files only after you confirm it.
[docs/architecture.md](docs/architecture.md) has the full picture.

## Further reading

- [vscode/README.md](vscode/README.md) — the extension in full: every command, settings, developing it
- [docs/configuration.md](docs/configuration.md) — every setting, and performance tuning
- [docs/architecture.md](docs/architecture.md) — the components, degraded mode, and startup step by step
- [docs/prep-skills.md](docs/prep-skills.md) — the prep skills in full
- [docs/wire-protocol.md](docs/wire-protocol.md) — the front end/backend message contract
- [CONTRIBUTING.md](CONTRIBUTING.md) — tests, project layout, house style

## Privacy

Pear reads the repo it's pointed at and sends parts of it to whichever
model you configured. Ollama stays on your machine; the Anthropic API, an
OpenAI-compatible endpoint, or Cline set up with a hosted provider sends code
to that provider. A remote STT/TTS service receives your recorded audio and
anything read aloud. Reviewing a pull request signs in to GitHub through VS
Code. Make sure that's acceptable for the code you review. The all-local
setup keeps everything on your machine.

## Licence

MIT — see [LICENSE](LICENSE).
