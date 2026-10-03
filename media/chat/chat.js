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
    actNow: { available: false, detail: "Checking whether a coding agent is set up…", agent: "the agent" },
    actMode: false,
    narrated: new Map(), // hunk index -> narration text already shown
    lastQuestion: new Map(), // hunk index -> the reviewer's latest question on it
    // "h:<index>" for a hunk, "f:<path>" for a file asked about: a divider marks each change.
    lastDivider: /** @type {string | null} */ (null),
    targetFile: /** @type {string | null} */ (null),
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

  // index -1 is a turn about a whole file (explore_reply), not a hunk.
  function appendDivider(info) {
    const isFile = info.index === -1;
    const divider = document.createElement("button");
    divider.className = "hunk-divider";
    divider.textContent = isFile
      ? `About ${info.file_path}`
      : `${info.file_path} — change ${info.index + 1}/${info.total}`;
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
    if (info && typeof info.index === "number" && info.file_path) {
      const key = info.index === -1 ? `f:${info.file_path}` : `h:${info.index}`;
      if (key !== state.lastDivider) {
        appendDivider(info);
        state.lastDivider = key;
      }
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
    // Questions about a file work before the review starts and after it ends.
    const canAsk = onHunk || state.targetFile !== null;
    /** @type {HTMLButtonElement} */ ($("send")).disabled = !canAsk;
    /** @type {HTMLButtonElement} */ (mic).disabled = !canAsk && !mic.classList.contains("recording");
    input.disabled = !canAsk;
    const act = /** @type {HTMLButtonElement} */ ($("act"));
    act.disabled = !onHunk || !state.actNow.available;
    act.title = state.actNow.available
      ? `Ask ${agentName()} to make a change. It proposes it as a diff; nothing is written until you apply it.`
      : `Act Now is off: ${state.actNow.detail} (Pear Review: Choose Coding Agent)`;
    act.classList.toggle("active", state.actMode);
    act.setAttribute("aria-pressed", String(state.actMode));
    input.placeholder = state.actMode
      ? `Tell ${agentName()} what to change… (nothing is written until you apply it)`
      : state.targetFile
        ? `Ask about ${state.targetFile}… (select lines to ask about them)`
        : "Ask about this change… (select lines in the editor to ask about them)";
    $("send").textContent = state.actMode ? "Send to agent" : "Send";
    document.querySelectorAll(".look-deeper").forEach(applyLookDeeper);
  }

  function agentName() {
    const agent = state.actNow.agent;
    return agent && agent !== "none" ? agent[0].toUpperCase() + agent.slice(1) : "the agent";
  }

  function showAgentWorking(what = "is working on it") {
    showThinking(`${agentName()} ${what} — this can take a minute…`, "deeper-pending");
  }

  // --- Act Now proposal ---------------------------------------------------------------
  // One card per proposal; a refined proposal replaces the live card's contents.

  let proposalCard = /** @type {HTMLElement | null} */ (null);

  function showProposal(p) {
    clearThinking();
    if (!proposalCard) {
      proposalCard = document.createElement("div");
      proposalCard.className = "turn proposal";
      transcript.appendChild(proposalCard);
    }
    const card = proposalCard;
    card.replaceChildren();
    const label = document.createElement("div");
    label.className = "role";
    label.textContent = `Proposed by ${p.agent} · not applied yet`;
    const summary = document.createElement("div");
    summary.className = "turn-body";
    summary.textContent = p.summary || "Proposed changes:";
    const files = document.createElement("div");
    files.className = "proposal-files";
    for (const f of p.files) {
      const b = document.createElement("button");
      b.className = "link";
      b.textContent = `${f.status} · ${f.file_path}`;
      b.title = "Open this file's proposed diff";
      b.addEventListener("click", () => post({ kind: "proposal", action: "open", file_path: f.file_path }));
      files.appendChild(b);
    }
    const actions = document.createElement("div");
    actions.className = "turn-actions";
    const apply = document.createElement("button");
    apply.textContent = "Apply";
    apply.title = "Write the proposed change to your working tree";
    apply.addEventListener("click", () => {
      post({ kind: "proposal", action: "apply" });
      setProposalDone("applying…", false);
    });
    const refineInput = document.createElement("input");
    refineInput.className = "refine";
    refineInput.placeholder = "What should change about it?";
    const refine = document.createElement("button");
    refine.className = "secondary";
    refine.textContent = "Refine";
    const sendRefine = () => {
      const text = refineInput.value.trim();
      if (!text) return refineInput.focus();
      post({ kind: "proposal", action: "refine", text });
      showAgentWorking("is refining its proposal");
    };
    refine.addEventListener("click", sendRefine);
    refineInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendRefine();
    });
    const discard = document.createElement("button");
    discard.className = "secondary";
    discard.textContent = "Discard";
    discard.addEventListener("click", () => {
      post({ kind: "proposal", action: "discard" });
      setProposalDone("discarded");
    });
    actions.append(apply, refineInput, refine, discard);
    card.append(label, summary, files, actions);
    card.scrollIntoView({ block: "end" });
  }

  // Locks the live card with a final status. `final` false keeps it live (Applying…),
  // so the success notice or an error can still settle it.
  function setProposalDone(status, final = true) {
    if (!proposalCard) return;
    const card = proposalCard;
    card.querySelectorAll("button:not(.link), input").forEach((el) => {
      /** @type {HTMLButtonElement} */ (el).disabled = true;
    });
    const label = card.querySelector(".role");
    if (label) label.textContent = (label.textContent || "").replace(/ · .*$/, ` · ${status}`);
    if (final) proposalCard = null;
  }

  // --- audio: a turn's clips play back to back; chunk_index 0 starts a new turn ----
  // The audio bar (Pause/Resume, Stop) shows while anything is being spoken. A markdown
  // file read aloud carries each clip's line range, which the editor highlights.

  const player = new Audio();
  /** @type {{ url: string, reading: any }[]} */
  let queue = [];
  let blocked = false; // autoplay refused until the panel has been clicked once
  let active = false; // a turn is being spoken, playing or paused
  let paused = false; // paused by the user
  let reading = /** @type {any} */ (null); // the file clip playing now, if any

  function updateAudioBar() {
    $("audio-bar").hidden = !active;
    $("audio-pause").textContent = paused ? "Resume" : "Pause";
    const what = reading ? `Reading ${reading.file_path}` : "Speaking";
    $("audio-status").textContent = paused ? `${what} — paused` : `${what}…`;
  }

  function enqueue(payload, readingInfo = null) {
    if (payload.chunk_index === 0) {
      // A new turn replaces whatever was speaking, paused or not.
      clearQueue();
      player.pause();
      paused = false;
      setReading(null);
    }
    const bytes = Uint8Array.from(atob(payload.audio_base64), (c) => c.charCodeAt(0));
    queue.push({ url: URL.createObjectURL(new Blob([bytes], { type: payload.mime_type })), reading: readingInfo });
    active = true;
    if (player.paused && !paused && !blocked) playNext();
    updateAudioBar();
  }

  function setReading(info) {
    if (!info && !reading) return;
    reading = info;
    post(info ? { kind: "reading", ...info } : { kind: "readingDone" });
  }

  function playNext() {
    const next = queue.shift();
    if (!next) return;
    player.src = next.url;
    setReading(next.reading);
    player.play().catch((err) => {
      if (err.name === "NotAllowedError") {
        queue.unshift(next);
        blocked = true;
        $("audio-blocked").hidden = false;
      }
    });
    updateAudioBar();
  }

  function clearQueue() {
    queue.forEach((entry) => URL.revokeObjectURL(entry.url));
    queue = [];
  }

  function stopAudio() {
    clearQueue();
    player.pause();
    active = false;
    paused = false;
    setReading(null);
    stopSpeaking();
    updateAudioBar();
  }

  player.addEventListener("ended", () => {
    URL.revokeObjectURL(player.src);
    if (queue.length) playNext();
    else stopAudio();
  });

  $("audio-pause").addEventListener("click", () => {
    paused = !paused;
    if (paused) player.pause();
    else if (player.src && !player.ended && player.currentTime > 0) void player.play();
    else playNext();
    updateAudioBar();
  });

  // Stop also stops the backend still synthesising the rest of a long file.
  $("audio-stop").addEventListener("click", () => {
    if (reading) post({ kind: "command", command: "interrupt" });
    stopAudio();
  });

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
    act_now_cleared(p) {
      setProposalDone("no changes left");
      appendTurn("system", p.message);
    },
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
    file_audio_chunk(p) {
      stopSpeaking();
      enqueue(p, { file_path: p.file_path, start_line: p.start_line, end_line: p.end_line });
    },
    turn_audio_chunk(p) {
      if (speaking) setSpeakState(speaking, "playing");
      enqueue(p);
    },
    notice(p) {
      // Only confirm_act_now sends a success notice.
      if (p.level === "success" && proposalCard) {
        proposalCard.classList.add("applied");
        setProposalDone("applied");
      }
      appendTurn("system", p.message);
    },
    error(p) {
      state.narrating = false;
      if (speaking?.dataset.mode === "loading") stopSpeaking();
      if (proposalCard) {
        proposalCard.querySelectorAll("button, input").forEach((el) => {
          /** @type {HTMLButtonElement} */ (el).disabled = false;
        });
      }
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
      if (!msg.recording) {
        if (state.actMode) showAgentWorking("will start once your words are transcribed");
        else showThinking("…transcribing");
      }
      updateControls();
    } else if (msg.kind === "target") {
      state.targetFile = msg.file_path;
      $("target").hidden = !msg.file_path;
      $("target-label").textContent = msg.file_path ? `Asking about ${msg.file_path}` : "";
      updateControls();
      if (msg.file_path) input.focus();
    } else if (msg.kind === "actMode") {
      state.actMode = msg.on;
      updateControls();
    } else if (msg.kind === "proposal") {
      showProposal(msg);
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

  $("act").addEventListener("click", () => post({ kind: "setActMode", on: !state.actMode }));

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
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("composer").dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  updateControls();
  post({ kind: "ready" });
})();
