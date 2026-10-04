# To do

## Move to the backend (AI_Pear_Reviewer), then re-pin

- [ ] **Launching for the extension.** `python/launch.py` imports `run.py`'s
      internals (`parse_args`, `find_free_port`, `REPO_PATH_ENV`), the preflight
      and `CONFIG`, so a backend refactor can break it with the backend's tests
      still green. Give `run.py` a supported mode that prints
      `PEAR_REVIEW_PORT=<port>` (e.g. `--announce-port`), with a backend test;
      the extension runs `backend/run.py` and `launch.py` goes.
- [ ] **Shared chat scripts.** `media/chat/blocks.js` and `media/chat/readalong.js`
      are ports of `static/js/md-preview.js` and `static/js/audio.js` ("keep the
      two in step"); the weighting maths is in a third place,
      `src/review/reading.ts`. Pull the pure parts into dependency-free modules
      the web app imports and the extension copies at build (as it does
      codicons), then delete the copies.
- [ ] **Test config without text patching.** `test/integration/fixtures.ts`
      rewrites `config.yaml` by string replacement. A config override the
      backend supports (a path or environment variable) would survive the
      file's formatting.

## Before sharing

- [ ] Push `feature/vscode-recording` (at `2f658f1`): the submodule pin isn't on
      GitHub, so a fresh clone and CI can't fetch `backend/`.
- [ ] A remote for this repository, then `repository` in package.json (and drop
      `--allow-missing-repository` / `--no-rewrite-relative-links`).
- [ ] A 128×128 PNG icon, and a publisher, for the Marketplace.
- [ ] The backend's 22 mypy errors (all pre-existing): its CI runs mypy.

## Not yet verified

- [ ] **Set Up Python Environment** end to end, from the `.vsix` in a clean
      VS Code profile.
- [ ] The manual F5 walkthrough (README, "Developing"), and
      `npm run test:integration:ollama`.
- [ ] Multi-root test: the first repository showed a sixth changed file after
      switching away and back. Find out what it is.

## Code

- [x] `chatPanel.register` takes 8 positional parameters, `voice.register` 5:
      pass a deps object.
- [ ] Keep the chat transcript across window reloads (webview `getState` /
      `setState`); it lives only while the view is retained.
- [x] Type `context_too_large` in `protocol.ts` (the chat reads it; it's still
      `Unknown`).
- [x] Run ruff on `python/` and `test/python/` in CI, with the backend's settings.

## Docs and comments

- [x] README step 2 says Start Review makes the reviewer "explain each change as
      you go"; explaining is on request by default now (step 4).
- [x] README "Tests": says three layers, misses `test/python` and
      `test:package`, and the `:ollama` run covers only the main workspace.
- [x] README step 4 has an unwrapped line; step 10 and the paragraph after
      step 12 pack several topics each, and say more than a reader needs.
- [x] `src/backend/backend.ts`: "Errors are shown in the chat panel too":
      they're notifications now (`notices.ts`).
- [x] `src/ui/chatPanel.ts`: "the rest arrive as their UI is built": the
      other messages are shown elsewhere by design.
- [x] `src/backend/python.ts`: the module comment lists the lookup order the
      function already spells out; keep only why `python` on PATH isn't used.
- [x] `docs/STYLE.md` §3: "as `files.ts` was into …" is changelog; cut it.
