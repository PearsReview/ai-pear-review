// The chat webview. Talks only to the extension (src/ui/chatPanel.ts), never to the
// backend; the message kinds here are ToWebview / FromWebview in that file.
// Transcript behaviour follows the browser's static/js/transcript.js: turns append,
// a divider marks each change of hunk, persona turns get Look deeper and a speaker.
// @ts-check
(function () {
  const vscode = acquireVsCodeApi();
  const { renderBlocks } = window.PearBlocks;
  const readAlong = window.PearReadAlong;
  const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
  const transcript = $("transcript");
  const header = $("hunk");
  const input = /** @type {HTMLTextAreaElement} */ ($("input"));
  const mic = $("mic");

  const state = {
    current: /** @type {number | null} */ (null),
    started: false,
    ended: false,
    narrationAvailable: true,
    narrating: false,
    actNow: { available: false, detail: "Checking whether a coding agent is set up…", agent: "the agent" },
    actMode: false,
    // A pull request review: nothing is written, so there's no Act Now.
    readOnly: false,
    stt: true,
    narrated: new Map(), // hunk index -> narration text already shown
    lastQuestion: new Map(), // hunk index -> the reviewer's latest question on it
    // "h:<index>" for a hunk, "f:<path>" for a file asked about: a divider marks each change.
    lastDivider: /** @type {string | null} */ (null),
    targetFile: /** @type {string | null} */ (null),
    // The file the hunk on screen is in, and whether the chat shows only its turns.
    reviewFile: /** @type {string | null} */ (null),
    fileFilter: false,
    // Lines are selected in the editor, to go with the next question.
    selection: false,
  };

  // The web app's suggestion lists (backend/static/js/interactions.js). A chip fills the
  // message box, to edit or send; nothing is sent until the reviewer does.
  const SUGGESTIONS = {
    file: ["Explain what this file does", "Summarize the changes in this file", "Are there any bugs here?"],
    hunk: ["Explain this change", "Why was this modified?", "Suggest a test for this"],
    selection: ["Explain this code", "Suggest a better approach", "Add a test for this selection"],
    // Instructions for the agent, not questions.
    actNow: [
      "Add a docstring to this function",
      "Add type hints",
      "Extract this into a named constant",
      "Add a null/None check",
      "Rename for clarity",
    ],
  };

  const post = (message) => vscode.postMessage(message);

  // Buttons are VS Code codicons (media/chat/codicons). An icon-only button carries its
  // name in aria-label and title; a pill shows a short label beside its icon.
  function icon(name, extra = "") {
    const i = document.createElement("i");
    i.className = `codicon codicon-${name} ${extra}`.trim();
    i.setAttribute("aria-hidden", "true");
    return i;
  }

  function iconButton(name, label, className = "icon-btn") {
    const b = document.createElement("button");
    b.className = className;
    setIcon(b, name, label);
    return b;
  }

  function pill(name, label, className = "") {
    const b = document.createElement("button");
    b.className = `pill ${className}`.trim();
    const text = document.createElement("span");
    text.textContent = label;
    b.append(icon(name), text);
    return b;
  }

  // Swaps an icon-only button's glyph and name together.
  function setIcon(button, name, label, spin = false) {
    button.replaceChildren(icon(name, spin ? "codicon-modifier-spin" : ""));
    button.title = label;
    button.setAttribute("aria-label", label);
  }

  // --- thinking --------------------------------------------------------------------

  let thinking = /** @type {HTMLElement | null} */ (null);
  let thinkingTimer = 0;

  function showThinking(text = "Thinking", kind = "") {
    clearThinking();
    thinking = document.createElement("div");
    thinking.className = `turn thinking ${kind}`;
    thinking.setAttribute("role", "status");
    const dots = document.createElement("span");
    dots.className = "dots";
    dots.setAttribute("aria-hidden", "true");
    dots.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
    const label = document.createElement("span");
    label.className = "grow";
    label.textContent = text;
    const stop = pill("debug-stop", "Interrupt", "subtle");
    stop.addEventListener("click", () => post({ kind: "command", command: "interrupt" }));
    thinking.append(dots, label, stop);
    transcript.appendChild(thinking);
    thinking.scrollIntoView({ block: "end" });
    return label;
  }

  function showDeeperPending() {
    const label = showThinking("", "deeper-pending");
    const started = Date.now();
    const tick = () => {
      const s = Math.round((Date.now() - started) / 1000);
      label.textContent = `Looking deeper — your coding agent is reading the repository (read-only). This takes longer than a normal reply… ${s}s`;
    };
    tick();
    thinkingTimer = window.setInterval(tick, 1000);
  }

  function clearThinking() {
    window.clearInterval(thinkingTimer);
    thinking?.remove();
    thinking = null;
  }

  // --- transcript ------------------------------------------------------------------

  // index -1 is a turn about a whole file (explore_reply), not a hunk.
  function appendDivider(info) {
    const isFile = info.index === -1;
    const divider = document.createElement("button");
    divider.className = "hunk-divider";
    divider.dataset.file = info.file_path;
    const label = document.createElement("span");
    label.textContent = isFile
      ? `About ${info.file_path}`
      : `${info.file_path} — change ${info.index + 1}/${info.total}`;
    divider.append(icon(isFile ? "file" : "git-compare"), label);
    divider.title = isFile ? `Open ${info.file_path}` : `Jump back to ${info.file_path}`;
    divider.addEventListener("click", () =>
      post(isFile ? { kind: "openFile", file_path: info.file_path } : { kind: "jump", index: info.index }),
    );
    transcript.appendChild(divider);
  }

  function roleLabel(role, info) {
    if (role === "presenter") return "Reviewer";
    if (role === "deeper")
      return `Looked deeper · ${info.agent} · ${info.model || "agent's default model"} · read-only`;
    if (role === "reviewer") return "You";
    if (role === "error") return "Error";
    return "System";
  }

  function lookDeeperButton(index, question) {
    const button = pill("search", "Look deeper", "subtle look-deeper");
    applyLookDeeper(button);
    button.addEventListener("click", () => {
      post(question ? { kind: "lookDeeper", index, question } : { kind: "lookDeeper", index });
      showDeeperPending();
    });
    return button;
  }

  function applyLookDeeper(button) {
    /** @type {HTMLButtonElement} */ (button).disabled = !state.actNow.available;
    button.title = state.actNow.available
      ? "A more thorough answer: your coding agent searches the whole repository and its history (read-only). Takes longer."
      : `Not available yet: ${state.actNow.detail}`;
  }

  // Each spoken message carries its own speaker button, and it is the only audio
  // control: idle (speaker) → loading (spinner, while speech is made) → playing (pause)
  // ⇄ paused (play) → idle when it finishes. The player (audio.js) knows which button's
  // audio it holds.
  // The newest reply's button: automatic narration audio plays under it.
  let latestSpeak = /** @type {HTMLElement | null} */ (null);

  const SPEAK_LOOK = {
    idle: ["unmute", "Read aloud"],
    loading: ["loading", "Getting speech… (click to cancel)"],
    playing: ["debug-pause", "Pause"],
    paused: ["play", "Play"],
  };

  function setSpeakState(button, mode) {
    button.dataset.mode = mode;
    button.classList.toggle("active", mode === "playing" || mode === "paused");
    const [glyph, label] = SPEAK_LOOK[mode];
    setIcon(button, glyph, label, mode === "loading");
  }

  const audio = window.PearAudio.createAudio({ post, readAlong, setSpeakState });

  // `request` asks for this message's speech again (posted when idle is clicked).
  function speakButton(request) {
    const button = iconButton("unmute", "Read aloud", "icon-btn speak");
    setSpeakState(button, "idle");
    button.addEventListener("click", () => {
      const mode = button.dataset.mode;
      if (mode === "playing") return audio.pause();
      if (mode === "paused") return audio.resume();
      if (mode === "loading") {
        post({ kind: "command", command: "interrupt" });
        return audio.stop();
      }
      audio.claim(button);
      post(request);
    });
    return button;
  }

  function appendTurn(role, text, info) {
    clearThinking();
    if (info && typeof info.index === "number" && info.file_path) {
      const key = info.index === -1 ? `f:${info.file_path}` : `h:${info.index}`;
      if (key !== state.lastDivider) {
        appendDivider(info);
        state.lastDivider = key;
      }
    }
    const turn = document.createElement("div");
    turn.className = `turn ${role}`;
    if (info && info.file_path) turn.dataset.file = info.file_path;
    const label = document.createElement("div");
    label.className = "role";
    label.textContent = roleLabel(role, info);
    const body = document.createElement("div");
    body.className = "turn-body";
    if (info && info.blocks && info.blocks.length) renderBlocks(body, info.blocks);
    else body.textContent = text;
    turn.append(label, body);

    if (info && info.related && info.related.length) {
      const row = document.createElement("div");
      row.className = "related";
      row.textContent = "Related: ";
      for (const r of info.related) {
        const chip = document.createElement("button");
        chip.className = "chip";
        chip.textContent = `${r.relation.replace(/_/g, " ")} · ${r.file_path}`;
        chip.title = r.note ? `${r.note} — jump to ${r.file_path}` : `Jump to ${r.file_path}`;
        chip.addEventListener("click", () => post({ kind: "jump", index: r.index }));
        row.appendChild(chip);
      }
      turn.appendChild(row);
    }

    if (role === "presenter" || role === "deeper") {
      const actions = document.createElement("div");
      actions.className = "turn-actions";
      const spoken = (info && info.spoken) || text;
      if (spoken && spoken.trim()) {
        latestSpeak = speakButton({ kind: "speak", text: spoken });
        actions.appendChild(latestSpeak);
      }
      if (role === "presenter" && info && info.index >= 0) {
        // A narration answers no question; Look deeper then asks the server's default.
        const question = "narration_available" in info ? null : state.lastQuestion.get(info.index);
        actions.appendChild(lookDeeperButton(info.index, question));
      }
      turn.appendChild(actions);
    }
    transcript.appendChild(turn);
    turn.scrollIntoView({ block: "end" });
    applyFilter();
  }

  // --- controls ----------------------------------------------------------------------

  function updateControls() {
    // A change on screen can be asked about before the review starts and after it ends.
    const onHunk = state.current !== null;
    /** @type {HTMLButtonElement} */ ($("explain")).disabled =
      !onHunk || !state.started || !state.narrationAvailable || state.narrating || state.narrated.has(state.current);
    const canAsk = onHunk || state.targetFile !== null;
    /** @type {HTMLButtonElement} */ ($("send")).disabled = !canAsk;
    const recording = mic.classList.contains("recording");
    /** @type {HTMLButtonElement} */ (mic).disabled = (!canAsk || !state.stt) && !recording;
    if (!recording) {
      setIcon(mic, "mic", state.stt ? "Push to talk (Ctrl+Alt+Space)" : "Voice input is off (Settings)");
    }
    input.disabled = !canAsk;
    const act = /** @type {HTMLButtonElement} */ ($("act"));
    act.hidden = state.readOnly;
    // Act Now changes code, which belongs to an open review.
    act.disabled = !onHunk || !state.actNow.available || state.ended;
    act.title = state.ended
      ? "Act Now is off: the review has ended. Reopen it to make changes."
      : state.actNow.available
        ? `Ask ${agentName()} to make a change. It proposes it as a diff; nothing is written until you apply it.`
        : `Act Now is off: ${state.actNow.detail} Choose an agent in settings (⚙).`;
    act.classList.toggle("active", state.actMode);
    act.setAttribute("aria-pressed", String(state.actMode));
    input.placeholder = state.actMode
      ? `Tell ${agentName()} what to change… (nothing is written until you apply it)`
      : state.targetFile
        ? `Ask about ${state.targetFile}… (select lines to ask about them)`
        : "Ask about this change… (select lines in the editor to ask about them)";
    setIcon($("send"), "send", state.actMode ? "Send to agent" : "Send");
    document.querySelectorAll(".look-deeper").forEach(applyLookDeeper);
    renderSuggestions();
  }

  // Which list applies now: act mode's instructions, then a selection's (the most
  // specific question), then the file asked about, then the change on screen.
  function renderSuggestions() {
    const box = $("suggestions");
    const canAsk = state.current !== null || state.targetFile !== null;
    const list = !canAsk
      ? null
      : state.actMode
        ? SUGGESTIONS.actNow
        : state.selection
          ? SUGGESTIONS.selection
          : state.targetFile
            ? SUGGESTIONS.file
            : SUGGESTIONS.hunk;
    box.hidden = !list;
    box.replaceChildren(
      ...(list ?? []).map((text) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "pill subtle suggestion";
        chip.textContent = text;
        chip.title = "Put this in the message box to edit or send";
        chip.addEventListener("click", () => {
          input.value = text;
          input.focus();
          input.setSelectionRange(text.length, text.length);
        });
        return chip;
      }),
    );
  }

  function agentName() {
    const agent = state.actNow.agent;
    return agent && agent !== "none" ? agent[0].toUpperCase() + agent.slice(1) : "the agent";
  }

  function showAgentWorking(what = "is working on it") {
    showThinking(`${agentName()} ${what} — this can take a minute…`, "deeper-pending");
  }

  // --- the file filter: only the turns about the file in view ---------------------------
  // The file asked about if there is one, else the hunk's. Untagged lines (thinking,
  // notices) always show: they are about whatever is happening now.
  function applyFilter() {
    const file = state.targetFile ?? state.reviewFile;
    const button = $("filter");
    button.setAttribute("aria-pressed", String(state.fileFilter));
    button.classList.toggle("active", state.fileFilter);
    for (const node of transcript.children) {
      if (!(node instanceof HTMLElement)) continue;
      const hide = state.fileFilter && !!node.dataset.file && node.dataset.file !== file;
      node.classList.toggle("filtered", hide);
    }
  }

  // --- too large for the model: a hand-off to an agent that can read the whole change ---

  function showTooLarge(p) {
    clearThinking();
    const card = document.createElement("div");
    card.className = "turn too-large";
    if (p.file_path) card.dataset.file = p.file_path;
    const label = document.createElement("div");
    label.className = "role";
    label.textContent =
      p.reason === "call_failed" ? "The model couldn't answer this in full" : "Too large for the model";
    const body = document.createElement("div");
    body.className = "turn-body";
    const size =
      p.estimated_tokens && p.budget_tokens
        ? ` (about ${p.estimated_tokens.toLocaleString()} tokens; it can read ${p.budget_tokens.toLocaleString()})`
        : "";
    body.textContent =
      (p.reason === "call_failed"
        ? "The model's answer failed, so this came from the prep briefing instead."
        : `${p.file_path ?? "This change"} doesn't fit in the model's context${size}.`) +
      " A coding agent can read the whole thing: copy the request for it.";
    const actions = document.createElement("div");
    actions.className = "turn-actions";
    const copy = pill("copy", "Copy for your agent", "strong");
    copy.addEventListener("click", () => post({ kind: "copy", text: p.handoff_text }));
    actions.appendChild(copy);
    if (p.kind === "hunk" && state.current !== null && state.actNow.available) {
      const deeper = pill("search", "Look deeper", "subtle");
      const index = state.current;
      deeper.addEventListener("click", () => {
        post(p.question ? { kind: "lookDeeper", index, question: p.question } : { kind: "lookDeeper", index });
        showDeeperPending();
      });
      actions.appendChild(deeper);
    }
    card.append(label, body, actions);
    transcript.appendChild(card);
    card.scrollIntoView({ block: "end" });
  }

  // --- extension → webview -----------------------------------------------------------

  const handlers = {
    presenting(p) {
      if (p.done) {
        state.current = null;
        header.textContent = p.ended
          ? "The review has ended."
          : p.total
            ? "End of the changes."
            : "No changes to review.";
        state.ended = !!p.ended || state.ended;
      } else {
        state.current = p.index;
        state.started = !!p.review_started;
        state.ended = !!p.review_ended;
        state.narrationAvailable = p.narration_available !== false;
        state.narrating = !!p.narrating && !state.narrated.has(p.index);
        header.textContent = `${p.file_path}  ·  change ${p.index + 1} of ${p.total}${state.ended ? "  ·  review ended" : ""}`;
        state.reviewFile = p.file_path;
        applyFilter();
        if (state.narrating) showThinking();
        else if (!state.started && !transcript.querySelector(".turn")) {
          appendTurn(
            "system",
            "Ask about this change below, or select lines in the editor to ask about them. To have the reviewer explain each change as you go, start the review from the Changes view (▶).",
          );
        }
      }
      updateControls();
    },
    narration(p) {
      state.narrating = false;
      const seen = state.narrated.get(p.index) === p.text;
      state.narrated.set(p.index, p.text);
      if (seen) clearThinking();
      else appendTurn(p.narration_available === false ? "system" : "presenter", p.text, p);
      updateControls();
    },
    human_turn(p) {
      if (typeof p.index === "number") state.lastQuestion.set(p.index, p.text);
      appendTurn("reviewer", p.text, p);
      showThinking();
    },
    reviewer_turn: (p) => appendTurn("presenter", p.text, p),
    deeper_turn: (p) => appendTurn("deeper", p.text, p),
    service_status(p) {
      if (p.tts === false) audio.stop();
      if (typeof p.read_only === "boolean") state.readOnly = p.read_only;
      if (p.act_now) state.actNow = p.act_now;
      if (p.act_now || typeof p.read_only === "boolean") updateControls();
    },
    // Narration and replies spoken automatically: under the newest reply's button.
    audio_chunk: (p) => audio.enqueue(p, latestSpeak),
    // A message read aloud on request: under the button that asked.
    turn_audio_chunk: (p) => audio.enqueue(p, p.chunk_index === 0 ? (audio.owner ?? latestSpeak) : audio.owner),
    context_too_large: (p) => showTooLarge(p),
  };

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.kind === "server") handlers[msg.message.type]?.(msg.message.payload);
    else if (msg.kind === "recording") {
      mic.classList.toggle("recording", msg.recording);
      setIcon(mic, msg.recording ? "mic-filled" : "mic", msg.recording ? "Stop recording and send" : "Push to talk");
      if (!msg.recording) {
        if (state.actMode) showAgentWorking("will start once your words are transcribed");
        else showThinking("…transcribing");
      }
      updateControls();
    } else if (msg.kind === "target") {
      state.targetFile = msg.file_path;
      applyFilter();
      $("target").hidden = !msg.file_path;
      $("target-label").textContent = msg.file_path ? `Asking about ${msg.file_path}` : "";
      updateControls();
      if (msg.file_path) input.focus();
    } else if (msg.kind === "prefs") {
      state.stt = msg.prefs.stt;
      updateControls();
    } else if (msg.kind === "actMode") {
      state.actMode = msg.on;
      updateControls();
    } else if (msg.kind === "stopAudio") {
      audio.stop();
    } else if (msg.kind === "explaining") {
      if (!state.narrating) {
        state.narrating = true;
        showThinking();
        updateControls();
      }
    } else if (msg.kind === "settle") {
      clearThinking();
      state.narrating = false;
      if (audio.owner?.dataset.mode === "loading") audio.stop();
      updateControls();
    } else if (msg.kind === "context") {
      $("context").hidden = !msg.label;
      $("context-label").textContent = msg.label ? `Asking about ${msg.label}` : "";
      state.selection = !!msg.label;
      renderSuggestions();
    } else if (msg.kind === "backend") {
      document.body.dataset.backend = msg.state;
      if (msg.state === "starting") header.textContent = "Starting the review backend…";
      if (msg.state === "error") {
        clearThinking();
        header.textContent = "The backend stopped. Run Pear Review: Show Log for details.";
      }
      if (msg.state !== "ready") {
        state.current = null;
        updateControls();
      }
    }
  });

  // --- webview → extension -------------------------------------------------------------

  document.querySelectorAll("[data-command]").forEach((el) =>
    el.addEventListener("click", () => {
      const command = el.getAttribute("data-command");
      post({ kind: "command", command });
      if (command === "explain") {
        state.narrating = true;
        showThinking();
        updateControls();
      }
    }),
  );

  $("context-clear").addEventListener("click", () => post({ kind: "clearContext" }));

  $("act").addEventListener("click", () => post({ kind: "setActMode", on: !state.actMode }));
  $("filter").addEventListener("click", () => {
    state.fileFilter = !state.fileFilter;
    applyFilter();
  });

  $("target-back").addEventListener("click", () => post({ kind: "backToReview" }));

  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || input.disabled) return;
    input.value = "";
    if (state.actMode) {
      post({ kind: "actNow", text });
      showAgentWorking();
    } else {
      post({ kind: "send", text });
      showThinking();
    }
  });

  input.addEventListener("keydown", (e) => {
    // isComposing: this Enter commits an IME candidate (Chinese, Japanese, Korean
    // input), it doesn't send the half-typed message.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("composer").dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  updateControls();
  post({ kind: "ready" });
})();
