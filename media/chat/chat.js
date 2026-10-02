// The chat webview. Talks only to the extension (src/ui/chatPanel.ts), never to the
// backend; the message kinds here are ToWebview / FromWebview in that file.
// @ts-check
(function () {
  const vscode = acquireVsCodeApi();
  const transcript = /** @type {HTMLElement} */ (document.getElementById("transcript"));
  const hunk = /** @type {HTMLElement} */ (document.getElementById("hunk"));
  const input = /** @type {HTMLTextAreaElement} */ (document.getElementById("input"));
  const mic = /** @type {HTMLButtonElement} */ (document.getElementById("mic"));
  const audioBlocked = /** @type {HTMLElement} */ (document.getElementById("audio-blocked"));
  let thinking = /** @type {HTMLElement | null} */ (null);

  // --- transcript -----------------------------------------------------------------

  function addTurn(role, text) {
    clearThinking();
    const turn = document.createElement("div");
    turn.className = `turn ${role}`;
    turn.textContent = text;
    transcript.appendChild(turn);
    turn.scrollIntoView({ block: "end" });
  }

  function showThinking() {
    clearThinking();
    thinking = document.createElement("div");
    thinking.className = "turn thinking";
    thinking.textContent = "…thinking";
    transcript.appendChild(thinking);
    thinking.scrollIntoView({ block: "end" });
  }

  function clearThinking() {
    thinking?.remove();
    thinking = null;
  }

  // --- audio: clips of one turn play back to back; chunk_index 0 starts a new turn ---

  const player = new Audio();
  /** @type {string[]} */
  let queue = [];
  let pendingPlay = false;

  function enqueue(payload) {
    if (payload.chunk_index === 0) {
      queue = [];
      player.pause();
    }
    const bytes = Uint8Array.from(atob(payload.audio_base64), (c) => c.charCodeAt(0));
    queue.push(URL.createObjectURL(new Blob([bytes], { type: payload.mime_type })));
    if (player.paused) playNext();
  }

  function playNext() {
    const url = queue.shift();
    if (!url) return;
    player.src = url;
    player.play().catch((err) => {
      // Autoplay is refused until the user has interacted with the panel once.
      if (err.name === "NotAllowedError") {
        queue.unshift(url);
        pendingPlay = true;
        audioBlocked.hidden = false;
      }
    });
  }

  player.addEventListener("ended", () => {
    URL.revokeObjectURL(player.src);
    playNext();
  });

  document.getElementById("enable-audio")?.addEventListener("click", () => {
    audioBlocked.hidden = true;
    if (pendingPlay) {
      pendingPlay = false;
      playNext();
    }
  });

  // --- extension → webview ----------------------------------------------------------

  const handlers = {
    presenting(p) {
      if (p.done) {
        hunk.textContent = p.total ? "End of the changes." : "No changes to review.";
        return;
      }
      hunk.textContent = `${p.file_path}  ·  hunk ${p.index + 1} of ${p.total}`;
      transcript.replaceChildren();
      if (!p.review_started) addTurn("system", "Press Start review to have the reviewer explain each change.");
      else if (p.narrating) showThinking();
    },
    narration: (p) => addTurn("reviewer", p.text),
    human_turn(p) {
      addTurn("human", p.text);
      showThinking();
    },
    reviewer_turn: (p) => addTurn("reviewer", p.text),
    audio_chunk: enqueue,
    turn_audio_chunk: enqueue,
    notice: (p) => addTurn("system", p.message),
    error: (p) => addTurn("error", p.message),
    context_too_large: () => addTurn("error", "This change is too large for the model's context."),
  };

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (msg.kind === "server") handlers[msg.message.type]?.(msg.message.payload);
    else if (msg.kind === "recording") {
      mic.classList.toggle("recording", msg.recording);
      mic.textContent = msg.recording ? "Stop" : "Mic";
      if (!msg.recording) showThinking();
    } else if (msg.kind === "backend") {
      document.body.dataset.backend = msg.state;
      if (msg.state === "starting") hunk.textContent = "Starting the review backend…";
      if (msg.state === "error") hunk.textContent = "The backend stopped. See Pear Review: Show Log.";
    }
  });

  // --- webview → extension ----------------------------------------------------------

  document
    .querySelectorAll("[data-command]")
    .forEach((el) =>
      el.addEventListener("click", () =>
        vscode.postMessage({ kind: "command", command: el.getAttribute("data-command") }),
      ),
    );

  document.getElementById("composer")?.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    vscode.postMessage({ kind: "send", text });
    input.value = "";
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      document.getElementById("composer")?.dispatchEvent(new Event("submit", { cancelable: true }));
    }
  });

  vscode.postMessage({ kind: "ready" });
})();
