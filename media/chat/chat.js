// The chat webview. Talks only to the extension (src/ui/chatPanel.ts), never to the
// backend; the message kinds here are ToWebview / FromWebview in that file.
// Transcript behaviour follows the browser's static/js/transcript.js: turns append,
// a divider marks each change of hunk, persona turns get Look deeper and a speaker.
// @ts-check
(function () {
  const vscode = acquireVsCodeApi();
  // @ts-ignore — set by blocks.js
  const { renderBlocks } = window.PearBlocks;
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
    actNow: { available: false, detail: "Checking whether a coding agent is set up…" },
    narrated: new Map(), // hunk index -> narration text already shown
    lastQuestion: new Map(), // hunk index -> the reviewer's latest question on it
    lastDivider: /** @type {number | null} */ (null),
  };

  const post = (message) => vscode.postMessage(message);

  // --- thinking --------------------------------------------------------------------

  let thinking = /** @type {HTMLElement | null} */ (null);
  let thinkingTimer = 0;

  function showThinking(text = "…thinking", kind = "") {
    clearThinking();
    thinking = document.createElement("div");
    thinking.className = `turn thinking ${kind}`;
    thinking.setAttribute("role", "status");
    const label = document.createElement("span");
    label.textContent = text;
    thinking.appendChild(label);
    const stop = document.createElement("button");
    stop.className = "link";
    stop.textContent = "Interrupt";
    stop.addEventListener("click", () => post({ kind: "command", command: "interrupt" }));
    thinking.appendChild(stop);
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

  function appendDivider(info) {
    const divider = document.createElement("button");
    divider.className = "hunk-divider";
    divider.textContent = `${info.file_path} — change ${info.index + 1}/${info.total}`;
    divider.title = `Jump back to ${info.file_path}`;
    divider.addEventListener("click", () => post({ kind: "jump", index: info.index }));
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
    const button = document.createElement("button");
    button.className = "secondary small look-deeper";
    button.textContent = "Look deeper";
    applyLookDeeper(button);
    button.addEventListener("click", () => {
      post(question ? { kind: "lookDeeper", index, question } : { kind: "lookDeeper", index });
      showDeeperPending();
    });
    return button;
  }

  function applyLookDeeper(button) {
    /** @type {HTMLButtonElement} */ (button).disabled = !state.actNow.available || state.ended;
    button.title = state.actNow.available
      ? "A more thorough answer: your coding agent searches the whole repository and its history (read-only). Takes longer."
      : `Not available yet: ${state.actNow.detail}`;
  }

  // The speaker button is idle (🔊), generating (⏳, until the first clip arrives) or
  // playing (⏹, click to stop). One reply at a time: starting another resets this one.
  let speaking = /** @type {HTMLElement | null} */ (null);

  function setSpeakState(button, mode) {
    button.dataset.mode = mode;
    button.classList.toggle("loading", mode === "loading");
    button.textContent = mode === "loading" ? "⏳" : mode === "playing" ? "⏹" : "🔊";
    const title =
      mode === "loading" ? "Generating speech…" : mode === "playing" ? "Stop reading" : "Read this reply aloud";
    button.title = title;
    button.setAttribute("aria-label", title);
  }

  function stopSpeaking() {
    if (speaking) setSpeakState(speaking, "idle");
    speaking = null;
  }

  function speakButton(spoken) {
    const button = document.createElement("button");
    button.className = "icon speak";
    setSpeakState(button, "idle");
    button.addEventListener("click", () => {
      if (speaking === button) {
        stopAudio();
        return;
      }
      stopAudio();
      speaking = button;
      setSpeakState(button, "loading");
      post({ kind: "speak", text: spoken });
    });
    return button;
  }

  function appendTurn(role, text, info) {
    clearThinking();
    if (info && typeof info.index === "number" && info.index >= 0 && info.index !== state.lastDivider) {
      appendDivider(info);
      state.lastDivider = info.index;
    }
    const turn = document.createElement("div");
    turn.className = `turn ${role}`;
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
      if (spoken && spoken.trim()) actions.appendChild(speakButton(spoken));
      if (role === "presenter" && info && info.index >= 0) {
        // A narration answers no question; Look deeper then asks the server's default.
        const question = "narration_available" in info ? null : state.lastQuestion.get(info.index);
        actions.appendChild(lookDeeperButton(info.index, question));
      }
      turn.appendChild(actions);
    }
    transcript.appendChild(turn);
    turn.scrollIntoView({ block: "end" });
  }

  // --- controls ----------------------------------------------------------------------

  function updateControls() {
    $("start").hidden = state.started;
    const onHunk = state.current !== null && !state.ended;
    /** @type {HTMLButtonElement} */ ($("explain")).disabled =
      !onHunk || !state.started || !state.narrationAvailable || state.narrating || state.narrated.has(state.current);
    /** @type {HTMLButtonElement} */ ($("send")).disabled = !onHunk;
    /** @type {HTMLButtonElement} */ (mic).disabled = !onHunk && !mic.classList.contains("recording");
    input.disabled = !onHunk;
    document.querySelectorAll(".look-deeper").forEach(applyLookDeeper);
  }

  // --- audio: a turn's clips play back to back; chunk_index 0 starts a new turn ----

  const player = new Audio();
  /** @type {string[]} */
  let queue = [];
  let blocked = false;

  function enqueue(payload) {
    if (payload.chunk_index === 0) {
      queue.forEach((url) => URL.revokeObjectURL(url));
      queue = [];
      player.pause();
    }
    const bytes = Uint8Array.from(atob(payload.audio_base64), (c) => c.charCodeAt(0));
    queue.push(URL.createObjectURL(new Blob([bytes], { type: payload.mime_type })));
    if (player.paused && !blocked) playNext();
  }

  function playNext() {
    const url = queue.shift();
    if (!url) return;
    player.src = url;
    player.play().catch((err) => {
      // Autoplay is refused until the user has interacted with the panel once.
      if (err.name === "NotAllowedError") {
        queue.unshift(url);
        blocked = true;
        $("audio-blocked").hidden = false;
      }
    });
  }

  player.addEventListener("ended", () => {
    URL.revokeObjectURL(player.src);
    if (queue.length) playNext();
    else stopSpeaking();
  });

  function stopAudio() {
    queue.forEach((url) => URL.revokeObjectURL(url));
    queue = [];
    player.pause();
    stopSpeaking();
  }

  $("enable-audio").addEventListener("click", () => {
    $("audio-blocked").hidden = true;
    blocked = false;
    playNext();
  });

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
        header.textContent = `${p.file_path}  ·  change ${p.index + 1} of ${p.total}`;
        if (state.narrating) showThinking();
        else if (!state.started && !transcript.querySelector(".turn")) {
          appendTurn("system", "Press Start review to have the reviewer explain each change.");
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
    agent_stopped: (p) => appendTurn("system", p.message),
    service_status(p) {
      if (p.tts === false) stopSpeaking();
      if (p.act_now) {
        state.actNow = p.act_now;
        updateControls();
      }
    },
    audio_chunk(p) {
      // Narration takes over the player, so a reply being read aloud stops.
      if (p.chunk_index === 0) stopSpeaking();
      enqueue(p);
    },
    turn_audio_chunk(p) {
      if (speaking) setSpeakState(speaking, "playing");
      enqueue(p);
    },
    notice: (p) => appendTurn("system", p.message),
    error(p) {
      state.narrating = false;
      if (speaking?.dataset.mode === "loading") stopSpeaking();
      appendTurn("error", p.message);
      updateControls();
    },
    context_too_large: () => appendTurn("error", "This change is too large for the model's context."),
  };

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.kind === "server") handlers[msg.message.type]?.(msg.message.payload);
    else if (msg.kind === "recording") {
      mic.classList.toggle("recording", msg.recording);
      mic.textContent = msg.recording ? "Stop" : "Mic";
      if (!msg.recording) showThinking("…transcribing");
      updateControls();
    } else if (msg.kind === "context") {
      $("context").hidden = !msg.label;
      $("context-label").textContent = msg.label ? `Asking about ${msg.label}` : "";
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

  $("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || input.disabled) return;
    post({ kind: "send", text });
    input.value = "";
    showThinking();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("composer").dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  updateControls();
  post({ kind: "ready" });
})();
