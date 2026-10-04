// The chat's speech player: one message's clips play back to back, under that message's
// speaker button (its "owner"), with the read-along highlight following the voice.
// chunk_index 0 starts a message, and its button takes the player; later clips that
// belong to a message no longer playing are dropped. (A markdown file read aloud is the
// extension's own: python/player.py.)
// @ts-check
(function () {
  /**
   * @param {{
   *   post: (message: Record<string, unknown>) => void,
   *   readAlong: PearReadAlong,
   *   setSpeakState: (button: HTMLElement, mode: SpeakMode) => void,
   * }} deps
   */
  function createAudio({ post, readAlong, setSpeakState }) {
    const player = new Audio();
    /** @type {HTMLElement | null} */
    let owner = null;
    /** @type {{ url: string, sentences: SentenceWeight[] | null }[]} */
    let queue = [];
    let paused = false;
    // The clip playing now: its sentences found in the message, for the read-along.
    /** @type {{ segments: ResolvedSentence[], at: number | null }} */
    let clip = { segments: [], at: null };

    /**
     * @param {{ chunk_index: number, audio_base64: string, mime_type: string, sentences?: unknown }} payload
     * @param {HTMLElement | null} button
     */
    function enqueue(payload, button) {
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
        sentences: Array.isArray(payload.sentences) ? payload.sentences : null,
      });
      if (owner && owner.dataset.mode !== "paused") setSpeakState(owner, "playing");
      if (player.paused && !paused) playNext();
    }

    function playNext() {
      const next = queue.shift();
      if (!next) return;
      player.src = next.url;
      // Resolve this clip's sentences against the message its button belongs to.
      const body = owner?.closest(".turn")?.querySelector(".turn-body");
      clip = {
        segments: next.sentences && body ? readAlong.resolveSentences(body, next.sentences) : [],
        at: null,
      };
      // Playing takes the voice: a file being read aloud stops (src/audio/speaking.ts).
      player
        .play()
        .then(() => post({ kind: "audioStarted" }))
        .catch((/** @type {Error} */ err) => {
          if (err.name === "NotAllowedError") {
            // The panel may not play sound until it has been clicked once: the button
            // waits on Play, and that click is the permission.
            queue.unshift(next);
            paused = true;
            if (owner) setSpeakState(owner, "paused");
            showBlockedHint(true);
            post({ kind: "audioBlocked" });
          }
        });
    }

    // Audio started from outside the panel (automatic narration before any click here)
    // is refused until the panel has been clicked once. Say so beside the button that is
    // waiting, rather than leave a silent play icon.
    /** @param {boolean} on */
    function showBlockedHint(on) {
      document.querySelectorAll(".audio-hint").forEach((el) => el.remove());
      if (!on || !owner) return;
      const hint = document.createElement("span");
      hint.className = "audio-hint";
      hint.textContent = "Press ▶ to start: the chat needs one click before it can play sound.";
      owner.after(hint);
    }

    function pause() {
      paused = true;
      player.pause();
      if (owner) setSpeakState(owner, "paused");
    }

    function resume() {
      paused = false;
      showBlockedHint(false);
      if (owner) setSpeakState(owner, "playing");
      if (player.src && !player.ended && player.currentTime > 0) {
        void player.play().then(() => post({ kind: "audioStarted" }));
      } else playNext();
    }

    function clearQueue() {
      queue.forEach((entry) => URL.revokeObjectURL(entry.url));
      queue = [];
    }

    function stop() {
      clearQueue();
      player.pause();
      paused = false;
      readAlong.highlight(null);
      showBlockedHint(false);
      clip = { segments: [], at: null };
      if (owner) setSpeakState(owner, "idle");
      owner = null;
    }

    // A button asks for its message's speech: it owns the player, waiting for clips.
    /** @param {HTMLElement} button */
    function claim(button) {
      stop();
      owner = button;
      setSpeakState(button, "loading");
    }

    // Where the voice is, estimated from how far through the clip it has got.
    player.addEventListener("timeupdate", () => {
      const duration = player.duration;
      if (!Number.isFinite(duration) || duration <= 0) return;
      const fraction = Math.min(1, Math.max(0, player.currentTime / duration));
      if (!clip.segments.length) return;
      const i = readAlong.blockAtFraction(clip.segments, fraction);
      if (i === null || i === clip.at) return;
      clip.at = i;
      readAlong.highlight(clip.segments[i]?.range ?? null);
      const turn = owner?.closest(".turn");
      if (turn instanceof HTMLElement) turn.dataset.readingSentence = String(i);
    });

    player.addEventListener("ended", () => {
      URL.revokeObjectURL(player.src);
      if (queue.length) playNext();
      else stop();
    });

    return {
      // The button whose message is in the player, if any.
      get owner() {
        return owner;
      },
      enqueue,
      pause,
      resume,
      stop,
      claim,
    };
  }

  window.PearAudio = { createAudio };
})();
