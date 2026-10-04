// The chat webview. Talks only to the extension (src/ui/chatPanel.ts), never to the
// backend; the message kinds here are ToWebview / FromWebview in that file.
// Transcript behaviour follows the browser's static/js/transcript.js: turns append,
// a divider marks each change of hunk, persona turns get Look deeper and a speaker.
// @ts-check
(function () {
  const vscode = acquireVsCodeApi();
  // @ts-ignore — set by blocks.js
  const { renderBlocks } = window.PearBlocks;
  // @ts-ignore — set by readalong.js
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
    stt: true,
    narrated: new Map(), // hunk index -> narration text already shown
    lastQuestion: new Map(), // hunk index -> the reviewer's latest question on it
    // "h:<index>" for a hunk, "f:<path>" for a file asked about: a divider marks each change.
    lastDivider: /** @type {string | null} */ (null),
    targetFile: /** @type {string | null} */ (null),
    // The file the hunk on screen is in, and whether the chat shows only its turns.
    reviewFile: /** @type {string | null} */ (null),
    fileFilter: false,
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
    /** @type {HTMLButtonElement} */ (button).disabled = !state.actNow.available || state.ended;
    button.title = state.actNow.available
      ? "A more thorough answer: your coding agent searches the whole repository and its history (read-only). Takes longer."
      : `Not available yet: ${state.actNow.detail}`;
  }

  // Each spoken message carries its own speaker button, and it is the only audio
  // control: idle (speaker) → loading (spinner, while speech is made) → playing (pause)
  // ⇄ paused (play) → idle when it finishes. The button whose audio is in the player is
  // the "owner"; starting another message's audio hands ownership over.
  let owner = /** @type {HTMLElement | null} */ (null);
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

  // `request` asks for this message's speech again (posted when idle is clicked).
  function speakButton(request) {
    const button = iconButton("unmute", "Read aloud", "icon-btn speak");
    setSpeakState(button, "idle");
    button.addEventListener("click", () => {
      const mode = button.dataset.mode;
      if (mode === "playing") return pause();
      if (mode === "paused") return resume();
      if (mode === "loading") {
        post({ kind: "command", command: "interrupt" });
        return stopAudio();
      }
      stopAudio();
      owner = button;
      setSpeakState(button, "loading");
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
    const onHunk = state.current !== null && !state.ended;
    /** @type {HTMLButtonElement} */ ($("explain")).disabled =
      !onHunk || !state.started || !state.narrationAvailable || state.narrating || state.narrated.has(state.current);
    // Questions about a file work before the review starts and after it ends.
    const canAsk = onHunk || state.targetFile !== null;
    /** @type {HTMLButtonElement} */ ($("send")).disabled = !canAsk;
    const recording = mic.classList.contains("recording");
    /** @type {HTMLButtonElement} */ (mic).disabled = (!canAsk || !state.stt) && !recording;
    if (!recording) {
      setIcon(mic, "mic", state.stt ? "Push to talk (Ctrl+Alt+Space)" : "Voice input is off (Settings)");
    }
    input.disabled = !canAsk;
    const act = /** @type {HTMLButtonElement} */ ($("act"));
    act.disabled = !onHunk || !state.actNow.available;
    act.title = state.actNow.available
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

  // --- the summary screen (send_summary_screen): where the review stands, what's next ---

  function showSummary(p) {
    clearThinking();
    document.querySelector(".turn.summary")?.remove();
    const card = document.createElement("div");
    card.className = "turn summary";
    const label = document.createElement("div");
    label.className = "role";
    label.textContent = p.ended_early ? "Review ended" : "Review finished";
    const body = document.createElement("div");
    body.className = "turn-body";
    const pending = p.pending_comment_count || 0;
    const parts = [`${p.reviewed_count ?? 0} of ${p.total} changes reviewed`];
    if (pending) parts.push(`${pending} comment${pending === 1 ? "" : "s"} waiting for a plan`);
    body.textContent = parts.join(" · ");
    const actions = document.createElement("div");
    actions.className = "turn-actions";
    if (pending) {
      const plan = pill("checklist", "Create plan", "strong");
      plan.addEventListener("click", () => post({ kind: "command", command: "createPlan" }));
      actions.appendChild(plan);
    }
    if (p.review_plan) {
      const open = pill("file", "Open plan");
      open.title = p.review_plan;
      open.addEventListener("click", () => post({ kind: "openPlan", file: p.review_plan }));
      actions.appendChild(open);
    }
    const again = pill("refresh", "Start new review", "subtle");
    again.addEventListener("click", () => post({ kind: "command", command: "newReview" }));
    actions.appendChild(again);
    card.append(label, body, actions);
    transcript.appendChild(card);
    card.scrollIntoView({ block: "end" });
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
      const glyph = f.status === "added" ? "diff-added" : f.status === "deleted" ? "diff-removed" : "diff-modified";
      const b = pill(glyph, `${f.status} · ${f.file_path}`, "file-link");
      b.title = "Open this file's proposed diff";
      b.addEventListener("click", () => post({ kind: "proposal", action: "open", file_path: f.file_path }));
      files.appendChild(b);
    }
    const actions = document.createElement("div");
    actions.className = "turn-actions";
    const apply = pill("check", "Apply", "strong");
    apply.title = "Write the proposed change to your working tree";
    apply.addEventListener("click", () => {
      post({ kind: "proposal", action: "apply" });
      setProposalDone("applying…", false);
    });
    const refineInput = document.createElement("input");
    refineInput.className = "refine";
    refineInput.placeholder = "What should change about it?";
    const refine = pill("edit", "Refine");
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
    const discard = pill("discard", "Discard", "subtle");
    discard.addEventListener("click", () => {
      post({ kind: "proposal", action: "discard" });
      setProposalDone("discarded");
    });
    // The refine box takes its own row; the three actions share the next.
    actions.append(refineInput, apply, refine, discard);
    card.append(label, summary, files, actions);
    card.scrollIntoView({ block: "end" });
  }

  // Locks the live card with a final status. `final` false keeps it live (Applying…),
  // so the success notice or an error can still settle it.
  function setProposalDone(status, final = true) {
    if (!proposalCard) return;
    const card = proposalCard;
    card.querySelectorAll("button:not(.file-link), input").forEach((el) => {
      /** @type {HTMLButtonElement} */ (el).disabled = true;
    });
    const label = card.querySelector(".role");
    if (label) label.textContent = (label.textContent || "").replace(/ · .*$/, ` · ${status}`);
    if (final) proposalCard = null;
  }

  // --- audio: one message's clips play back to back -----------------------------------
  // chunk_index 0 starts a message, and its button takes the player; later clips that
  // belong to a message no longer playing are dropped. A markdown file read aloud carries
  // each clip's line range, which the editor highlights.

  const player = new Audio();
  /** @type {{ url: string, reading: any, sentences: any[] | null, blocks: any[] | null }[]} */
  let queue = [];
  let paused = false;
  let reading = /** @type {any} */ (null); // the file passage playing now, if any
  // The clip playing now: its sentences found in the message (read-along), or for a
  // file read, its blocks with their lines.
  let clip = /** @type {{ segments: any[], blocks: any[] | null, file: string | null, at: any }} */ ({
    segments: [],
    blocks: null,
    file: null,
    at: null,
  });

  function enqueue(payload, button, readingInfo = null) {
    if (payload.chunk_index === 0) {
      clearQueue();
      player.pause();
      if (owner && owner !== button) setSpeakState(owner, "idle");
      owner = button;
      paused = false;
    } else if (button !== owner) {
      return;
    }
    const bytes = Uint8Array.from(atob(payload.audio_base64), (c) => c.charCodeAt(0));
    queue.push({
      url: URL.createObjectURL(new Blob([bytes], { type: payload.mime_type })),
      reading: readingInfo,
      sentences: Array.isArray(payload.sentences) ? payload.sentences : null,
      blocks: Array.isArray(payload.blocks) ? payload.blocks : null,
    });
    if (owner && owner.dataset.mode !== "paused") setSpeakState(owner, "playing");
    if (player.paused && !paused) playNext();
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
    // Resolve this clip's sentences against the message its button belongs to.
    const body = owner?.closest(".turn")?.querySelector(".turn-body");
    clip = {
      segments: next.sentences && body ? readAlong.resolveSentences(body, next.sentences) : [],
      blocks: next.blocks,
      file: next.reading ? next.reading.file_path : null,
      at: null,
    };
    player.play().catch((err) => {
      if (err.name === "NotAllowedError") {
        // The panel may not play sound until it has been clicked once: the button
        // waits on Play, and that click is the permission.
        queue.unshift(next);
        paused = true;
        if (owner) setSpeakState(owner, "paused");
      }
    });
  }

  function pause() {
    paused = true;
    player.pause();
    if (owner) setSpeakState(owner, "paused");
  }

  function resume() {
    paused = false;
    if (owner) setSpeakState(owner, "playing");
    if (player.src && !player.ended && player.currentTime > 0) void player.play();
    else playNext();
  }

  function clearQueue() {
    queue.forEach((entry) => URL.revokeObjectURL(entry.url));
    queue = [];
  }

  function stopAudio() {
    clearQueue();
    player.pause();
    paused = false;
    setReading(null);
    readAlong.highlight(null);
    clip = { segments: [], blocks: null, file: null, at: null };
    if (owner) setSpeakState(owner, "idle");
    owner = null;
  }

  // Where the voice is, estimated from how far through the clip it has got.
  function onTimeUpdate() {
    const duration = player.duration;
    if (!Number.isFinite(duration) || duration <= 0) return;
    const fraction = Math.min(1, Math.max(0, player.currentTime / duration));
    if (clip.segments.length) {
      const i = readAlong.blockAtFraction(clip.segments, fraction);
      if (i === clip.at) return;
      clip.at = i;
      readAlong.highlight(clip.segments[i]?.range ?? null);
      const turn = owner?.closest(".turn");
      if (turn instanceof HTMLElement) turn.dataset.readingSentence = String(i);
    } else if (clip.blocks && clip.blocks.length && clip.file) {
      // A file read: move the editor's highlight to the block being read.
      const i = readAlong.blockAtFraction(clip.blocks, fraction);
      if (i === clip.at) return;
      clip.at = i;
      const block = clip.blocks.find((b) => b.block_index === i);
      if (block) setReading({ file_path: clip.file, start_line: block.start_line, end_line: block.end_line });
    }
  }

  player.addEventListener("timeupdate", onTimeUpdate);

  player.addEventListener("ended", () => {
    URL.revokeObjectURL(player.src);
    if (queue.length) playNext();
    else stopAudio();
  });

  // --- a markdown file read aloud gets a line of its own, with the same button ---------

  const readingTurns = new Map(); // file path -> its "Reading" button, while it is live

  function readingTurn(filePath) {
    clearThinking();
    const turn = document.createElement("div");
    turn.className = "turn system reading";
    const label = document.createElement("span");
    label.className = "grow";
    label.textContent = `Reading ${filePath}`;
    const button = speakButton({ kind: "speakFile", file_path: filePath });
    turn.append(icon("book"), label, button);
    transcript.appendChild(turn);
    turn.scrollIntoView({ block: "end" });
    readingTurns.set(filePath, button);
    return button;
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
        if (p.ended) showSummary(p);
      } else {
        state.current = p.index;
        state.started = !!p.review_started;
        state.ended = !!p.review_ended;
        state.narrationAvailable = p.narration_available !== false;
        state.narrating = !!p.narrating && !state.narrated.has(p.index);
        header.textContent = `${p.file_path}  ·  change ${p.index + 1} of ${p.total}`;
        state.reviewFile = p.file_path;
        applyFilter();
        if (state.narrating) showThinking();
        else if (!state.started && !transcript.querySelector(".turn")) {
          appendTurn(
            "system",
            "Ask about this change below. To have the reviewer explain each change as you go, start the review from the Changes view (▶).",
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
    agent_stopped: (p) => appendTurn("system", p.message),
    act_now_cleared(p) {
      setProposalDone("no changes left");
      appendTurn("system", p.message);
    },
    service_status(p) {
      if (p.tts === false) stopAudio();
      if (p.act_now) {
        state.actNow = p.act_now;
        updateControls();
      }
    },
    // Narration and replies spoken automatically: under the newest reply's button.
    audio_chunk: (p) => enqueue(p, latestSpeak),
    // A message read aloud on request: under the button that asked.
    turn_audio_chunk: (p) => enqueue(p, p.chunk_index === 0 ? (owner ?? latestSpeak) : owner),
    file_audio_chunk(p) {
      const button = readingTurns.get(p.file_path) ?? readingTurn(p.file_path);
      if (p.chunk_index === 0 && owner !== button) {
        owner = button;
      }
      enqueue(p, button, { file_path: p.file_path, start_line: p.start_line, end_line: p.end_line });
    },
    notice(p) {
      // speak_file brackets a read with these two; its own line replaces them.
      const starting = /^Reading (.+)\.\.\.$/.exec(p.message);
      if (starting) {
        // Re-read from an existing line: that line takes the audio, no new one.
        const reuse = owner?.dataset.mode === "loading" && owner.closest(".reading") ? owner : undefined;
        const button = reuse ?? readingTurn(starting[1]);
        readingTurns.set(starting[1], button);
        owner = button;
        setSpeakState(button, "loading");
        return;
      }
      if (/^Finished reading /.test(p.message)) return;
      if (p.level === "success" && proposalCard && p.message.startsWith("Applied the change")) {
        proposalCard.classList.add("applied");
        setProposalDone("applied");
      }
      appendTurn("system", p.message);
    },
    error(p) {
      state.narrating = false;
      if (owner?.dataset.mode === "loading") stopAudio();
      if (proposalCard) {
        proposalCard.querySelectorAll("button, input").forEach((el) => {
          /** @type {HTMLButtonElement} */ (el).disabled = false;
        });
      }
      appendTurn("error", p.message);
      updateControls();
    },
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
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("composer").dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  updateControls();
  post({ kind: "ready" });
})();
