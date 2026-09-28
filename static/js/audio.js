// All audio playback: three queues over the one <audio> element, plus the
// read-along highlight that tracks which markdown block is being spoken.
//
// The queues stay separate rather than sharing one implementation — the
// file reader owns markdown-preview concerns (setMdReadingChunk,
// setSpeakingFile, updateMdPreviewBar) that are meaningless for narration
// and for the tour. See the comment on the narration queue below.
//
// Which outbound messages have to stop playback lives here too
// (stopAudioForSend), next to the queues it stops, rather than inside
// send() — that keeps the socket layer from having to know which message
// types belong to which feature.

import { state } from "./state.js";
import { audioEl, codeViewEl } from "./dom.js";
import { chunkCoversBlock, setSpeakingFile, updateMdPreviewBar } from "./md-preview.js";

// Narration/reply audio. A turn longer than the configured TTS budget
// arrives as several consecutive clips (see try_speak in web/speech.py), so
// this needs a queue for the same reason the file reader does — messages
// arrive far faster than clips play, and assigning audioEl.src per message
// would have each one cut off the last.
//
// Deliberately a SEPARATE queue from the file reader's rather than one
// shared implementation: playNextFileAudioChunk and enqueueFileAudioChunk
// own markdown-preview concerns (setMdReadingChunk, setSpeakingFile,
// updateMdPreviewBar) that are meaningless for narration. Sharing would
// mean threading an "is this a file chunk?" conditional through five
// functions, making the md-preview machinery polymorphic — the opposite of
// the separation the comment on the file queue describes. ~15 lines of
// similar-looking queue logic is the cheaper trade.
// Each clip is {src, segments}: segments are its sentences resolved to
// Ranges in the reply they belong to, for the read-along highlight below.
let narrationQueue = [];
let narrationPlaying = false;
let narrationClips = null; // the clips of the turn now receiving audio
let narrationTextIndex = null; // that turn's text, for finding sentences in it
// Every spoken turn's clips, for its speaker button to replay without
// synthesizing again. Weak, so a cleared transcript takes its audio with it.
const turnClips = new WeakMap();
// The turn whose speaker button asked for audio ("speak_turn"), which
// "turn_audio_chunk" clips belong to — not necessarily the latest turn.
let requestedTurnEl = null;
// How many clips the turn now receiving audio will have (its chunk_count)
// and how many have arrived — so running out of queued clips can be told
// apart from running ahead of synthesis. null for a replay: already complete.
let narrationExpected = null;
let narrationReceived = 0;

// --- Speaker button state ----------------------------------------------------
// The button of the turn whose audio is playing is lit ("is-active"), and
// shows a spinner ("is-loading") while speech is still being generated —
// after a click, or when playback has caught up with synthesis. One turn at
// a time, since there's one <audio> element. Every way audio ends — the last
// clip finishing, Stop, another turn starting, the TTS service failing —
// goes through setTurnAudioState(..., null) or stopNarrationQueue.
let activeTurnEl = null;

function setTurnAudioState(turnEl, mode) {
  if (activeTurnEl && activeTurnEl !== turnEl) paintSpeakButton(activeTurnEl, null);
  activeTurnEl = mode ? turnEl : null;
  if (turnEl) paintSpeakButton(turnEl, mode);
}

function paintSpeakButton(turnEl, mode) {
  const button = turnEl.querySelector(".speak-turn-btn");
  if (!button) return;
  button.classList.toggle("is-active", !!mode);
  button.classList.toggle("is-loading", mode === "loading");
  button.setAttribute("aria-pressed", String(!!mode));
  button.setAttribute("aria-busy", String(mode === "loading"));
  button.title = mode ? "Stop reading aloud" : "Read this reply aloud";
}

// Called when service_status reports TTS down: a clip that was still being
// generated is never coming, so stop waiting for it — play out whatever
// already arrived, then drop the highlight.
export function onTurnAudioUnavailable() {
  if (narrationExpected !== null) narrationExpected = narrationReceived;
  if (!narrationPlaying) setTurnAudioState(activeTurnEl, null);
}

// Automatic narration/reply audio, for the latest turn.
export function playAudio(payload) {
  // Defensive: the server already skips synthesizing/sending audio when the
  // reviewer's TTS preference is off (see sendVoicePrefs), but a message
  // already in flight when the preference flips shouldn't play anyway.
  if (!state.ttsPrefOn) return;
  receiveClip(payload, state.lastPresenterTurnEl);
}

// Audio a speaker button asked for — played whatever the voice preference
// says, since asking for it is the reviewer turning it on for this turn.
export function playTurnAudio(payload) {
  receiveClip(payload, requestedTurnEl);
}

// The speaker button on a presenter turn (see appendTurn). Replays the
// turn's audio if it has been spoken already; otherwise asks the server to
// speak `spoken`, the sanitised text it would have narrated.
export function speakTurn(turnEl, spoken, send) {
  if (activeTurnEl === turnEl) {
    // A lit button is a stop button. While speech is still being generated
    // the server has work to cancel; once it's all here, only the player does.
    if (narrationExpected !== null && narrationReceived < narrationExpected) send("stop");
    stopAllAudio();
    return;
  }
  const clips = turnClips.get(turnEl);
  if (clips && clips.length) {
    // Stop everything else first: one meaning — stop whatever else is
    // playing, play this turn. Going back through the queue rather than
    // assigning audioEl.src directly, which would play only the first clip.
    stopAllAudio();
    narrationQueue = clips.slice();
    setTurnAudioState(turnEl, "playing");
    playNextNarrationChunk();
    return;
  }
  requestedTurnEl = turnEl;
  send("speak_turn", { text: spoken });
  // After send(), which stops whatever was playing (stopAudioForSend) and
  // with it any other turn's lit button.
  narrationExpected = Infinity; // unknown until the first clip says
  narrationReceived = 0;
  setTurnAudioState(turnEl, "loading");
}

function receiveClip(payload, turnEl) {
  const src = `data:${payload.mime_type};base64,${payload.audio_base64}`;
  // Treat a missing index as 0 — a single unlabelled clip then behaves
  // exactly as it did before turns could be split.
  const index = payload.chunk_index || 0;

  if (index === 0) {
    // A new turn replaces whatever was still playing, which is precisely
    // what the old unconditional `audioEl.src = src` did. Keying that off
    // chunk_index === 0 is why the server always sends the field.
    stopNarrationQueue();
    narrationClips = [];
    narrationExpected = payload.chunk_count || 1;
    narrationReceived = 0;
    if (turnEl) setTurnAudioState(turnEl, "playing");
    // Bound to the array rather than a copy, so the speaker button replays
    // what has arrived so far — honest, since that's all that's been said.
    if (turnEl) turnClips.set(turnEl, narrationClips);
    const body = turnEl && turnEl.querySelector(".turn-body");
    narrationTextIndex = body ? buildTextIndex(body) : null;
  }
  if (!narrationClips) narrationClips = []; // a stray non-zero index
  // Resolved now, in arrival order, because each sentence is searched for
  // after the previous one — the same words can appear twice in a reply.
  const clip = { src, segments: resolveSegments(narrationTextIndex, payload.sentences) };
  narrationClips.push(clip);
  narrationReceived += 1;
  narrationQueue.push(clip);
  if (!narrationPlaying) playNextNarrationChunk();
}

function playNextNarrationChunk() {
  const clip = narrationQueue.shift();
  if (!clip) {
    narrationPlaying = false;
    audioEl.onended = null;
    audioEl.ontimeupdate = null;
    setSpokenSentence(null);
    // Caught up with synthesis: more is coming, so spin rather than go dark.
    const more = narrationExpected !== null && narrationReceived < narrationExpected;
    setTurnAudioState(activeTurnEl, more ? "loading" : null);
    if (!more) narrationExpected = null;
    return;
  }
  narrationPlaying = true;
  if (activeTurnEl) setTurnAudioState(activeTurnEl, "playing");
  audioEl.onended = playNextNarrationChunk;
  // Replaces any handler a file read left on the shared element, so it
  // can't keep firing against a chunk that's no longer playing.
  audioEl.ontimeupdate = () => onNarrationTimeUpdate(clip);
  setSpokenSentence(clip.segments.length ? clip.segments[0].range : null);
  audioEl.src = clip.src;
  audioEl.play().catch((err) => console.warn("audio playback blocked", err));
}

function stopNarrationQueue() {
  narrationQueue = [];
  narrationPlaying = false;
  narrationExpected = null;
  setTurnAudioState(activeTurnEl, null);
  audioEl.onended = null;
  audioEl.ontimeupdate = null;
  audioEl.pause();
  audioEl.currentTime = 0;
  setSpokenSentence(null);
}

// --- Chat read-along highlight ---------------------------------------------
// The sentence being spoken is highlighted in the reply, estimated the same
// way as the file reader's block highlight (see blockAtFraction below): how
// far through the clip we are, spread across its sentences by spoken length.
//
// Drawn with the CSS Custom Highlight API rather than by wrapping sentences
// in elements: a sentence can start in plain text and end inside `code` or
// **bold**, which no single wrapper element can cover, and the transcript's
// DOM stays exactly as appendTurn built it. Browsers without the API get
// no highlight and nothing else changes.
const CAN_HIGHLIGHT = typeof CSS !== "undefined" && !!CSS.highlights && typeof Highlight === "function";
const SPOKEN_HIGHLIGHT = "tts-reading";
let spokenRange = null;

function setSpokenSentence(range) {
  if (!CAN_HIGHLIGHT || range === spokenRange) return;
  spokenRange = range;
  if (range) CSS.highlights.set(SPOKEN_HIGHLIGHT, new Highlight(range));
  else CSS.highlights.delete(SPOKEN_HIGHLIGHT);
}

function onNarrationTimeUpdate(clip) {
  const duration = audioEl.duration;
  if (!clip.segments.length || !Number.isFinite(duration) || duration <= 0) return;
  const fraction = Math.min(1, Math.max(0, audioEl.currentTime / duration));
  setSpokenSentence(clip.segments[blockAtFraction(clip.segments, fraction)].range);
}

// Only letters and digits take part in the match, on both sides. The
// spoken text is derived from what's rendered but not identical to it:
// blocks are joined with blank lines where the page has adjacent elements,
// emoji and symbols are stripped for the voice (strip_unreadable_chars),
// headings and list items gain a "." (_ensure_pause), and a table row is
// spoken as "cell, cell, cell." while its cells render raw, markdown and
// all. Every one of those differences is whitespace, punctuation or a
// symbol, so ignoring those finds the sentence through all of them.
const MATCHABLE = /[\p{L}\p{N}]/u;

// Every matchable character of the reply as rendered, plus the text node
// and offset it came from.
function buildTextIndex(root) {
  let text = "";
  const where = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const data = node.data;
    for (let i = 0; i < data.length; i++) {
      if (!MATCHABLE.test(data[i])) continue;
      text += data[i];
      where.push([node, i]);
    }
  }
  return { root, text, where, cursor: 0 };
}

// Each sentence becomes {block_index, weight, range} — the shape
// blockAtFraction takes — with range null when the sentence isn't in the
// rendered reply (a code block is spoken as "(code block omitted)"). A
// sentence that can't be found keeps its weight, so the ones either side
// still line up with the audio.
function resolveSegments(index, sentences) {
  if (!Array.isArray(sentences)) return [];
  return sentences.map((s, i) => ({ block_index: i, weight: s.weight, range: index ? findSentence(index, s.text) : null }));
}

// What each sentence resolves to in `root`, as the text it would highlight
// (null if not found) — the matching above, runnable without any audio.
export function locateSentences(root, sentences) {
  const index = buildTextIndex(root);
  return sentences.map((s) => {
    const range = findSentence(index, s);
    return range ? range.toString() : null;
  });
}

function findSentence(index, sentence) {
  const needle = Array.from(String(sentence)).filter((c) => MATCHABLE.test(c)).join("");
  const at = needle ? index.text.indexOf(needle, index.cursor) : -1;
  if (at < 0) return null;
  index.cursor = at + needle.length;
  const [startNode, startOffset] = index.where[at];
  const [endNode, endOffset] = index.where[at + needle.length - 1];
  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(...extendOverClosing(index.root, endNode, endOffset + 1));
  return range;
}

// The match ends on the sentence's last letter or digit, so carry it over
// whatever closes the sentence — "fetch()." rather than "fetch" — even when
// that punctuation sits in the next text node (after a code span). Brackets
// count, for a call's "()". Stops at whitespace or anything else, so it never
// runs into the next sentence or table cell.
const CLOSING = /[.,;:!?()[\]{}"'’”]/;

function extendOverClosing(root, node, offset) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  walker.currentNode = node;
  for (;;) {
    while (offset < node.data.length && CLOSING.test(node.data[offset])) offset++;
    if (offset < node.data.length) return [node, offset];
    const next = walker.nextNode();
    if (!next || !next.data.length || !CLOSING.test(next.data[0])) return [node, offset];
    node = next;
    offset = 0;
  }
}

// Sequential playback queue for a multi-chunk file read ("speak_file"),
// kept separate from playAudio above: playAudio always plays exactly one
// clip immediately, which would clobber/interrupt itself if reused
// per-chunk here since "file_audio_chunk" messages can arrive faster than
// each clip takes to play. Shares the same <audio id="tts-audio"> element
// rather than adding a second one — narration/reply audio (playAudio) and
// a file read are mutually exclusive in practice, since cancel_current()
// runs server-side before starting either one, so starting a file read
// always cancels any in-flight narration generation first, and vice
// versa. A turn's speaker button replaying cached clips never reaches the
// server, so it calls stopAllAudio itself before taking the element (see
// speakTurn).
export let fileAudioQueue = [];
export let fileAudioPlaying = false;
// Held, not stopped: the clip keeps its currentTime and the queue keeps its
// chunks, so resuming carries on mid-sentence. Only the file read has this;
// a narration clip or a tour sentence is short enough that Stop is the
// whole vocabulary.
//
// fileAudioPlaying deliberately stays TRUE while paused, and two things
// downstream depend on that: enqueueFileAudioChunk's guard below (so a
// chunk arriving during a pause queues up instead of jumping ahead of the
// held one), and stopAudioForSend's audioInFlight (so navigating away still
// discards a paused read rather than leaving it to resume under the next
// hunk).
export let fileAudioPaused = false;

export function enqueueFileAudioChunk(payload) {
  if (!state.ttsPrefOn) return;
  fileAudioQueue.push(payload);
  if (!fileAudioPlaying) playNextFileAudioChunk();
  if (state.codeViewMode === "md-preview") updateMdPreviewBar(); // audio is in flight — offer Stop
  // The last chunk of this file has now been fully delivered — nothing
  // more is coming for it, so the icon can revert (a few seconds before
  // that chunk finishes *playing*, in exchange for not needing a second,
  // playback-completion-based signal for what's otherwise the same
  // moment from the user's perspective). Error/mid-read-failure
  // completion is handled separately by showError below, since a failed
  // read never sends this final chunk.
  if (payload.chunk_index === payload.chunk_count - 1 && state.speakingFilePath === payload.file_path) {
    setSpeakingFile(null);
  }
}

function playNextFileAudioChunk() {
  const next = fileAudioQueue.shift();
  if (!next) {
    fileAudioPlaying = false;
    setMdReadingChunk(null);
    if (state.codeViewMode === "md-preview") updateMdPreviewBar(); // playback finished — drop Stop
    return;
  }
  fileAudioPlaying = true;
  audioEl.onended = playNextFileAudioChunk;
  audioEl.ontimeupdate = onFileAudioTimeUpdate;
  // Bound to the chunk that's *starting to play*, not the one that just
  // arrived — chunks are delivered far faster than they play, so tracking
  // them at enqueue time would run the highlight ahead of the voice. This
  // is also where the estimate re-syncs exactly, every chunk boundary.
  setMdReadingChunk(next);
  audioEl.src = `data:${next.mime_type};base64,${next.audio_base64}`;
  audioEl.play().catch((err) => console.warn("audio playback blocked", err));
}

// Pause needs none of the queue machinery, because playback is a real
// <audio> element rather than window.speechSynthesis: the chain only
// advances on audioEl.onended, which cannot fire while the element is
// paused, and the read-along highlight is driven by ontimeupdate, which
// simply stops firing and picks up again at the same fraction. Holding the
// element is the entire feature.
export function pauseFileAudio() {
  if (!fileAudioPlaying || fileAudioPaused) return;
  fileAudioPaused = true;
  audioEl.pause();
  if (state.codeViewMode === "md-preview") updateMdPreviewBar();
}

export function resumeFileAudio() {
  if (!fileAudioPaused) return;
  fileAudioPaused = false;
  // A resume is a real click, so autoplay policy won't block it — but the
  // promise still must not reject unhandled, same as the three players.
  audioEl.play().catch((err) => console.warn("audio playback blocked", err));
  if (state.codeViewMode === "md-preview") updateMdPreviewBar();
}

// --- Read-along highlight -------------------------------------------------
// The TTS endpoint hands back one opaque audio blob per chunk with no word
// timings, so the position of the voice inside a chunk has to be estimated:
// how far through the audio we are, spread across the blocks by their
// spoken length (weights are measured on the humanized text server-side —
// see markdown_speech._weight — because that's what was actually
// synthesized). It drifts a little mid-chunk and is exactly right again at
// every chunk boundary, which is the trade for keeping chunks big enough
// that playback has no gaps.

// Pure, and deliberately top-level: it's the fiddliest bit of arithmetic
// here and this way it's testable without a live TTS service.
export function blockAtFraction(blocks, fraction) {
  if (!blocks || !blocks.length) return null;
  const total = blocks.reduce((sum, b) => sum + (b.weight || 0), 0);
  if (total <= 0) return blocks[0].block_index;
  const target = fraction * total;
  let acc = 0;
  for (const b of blocks) {
    acc += b.weight || 0;
    if (target <= acc) return b.block_index;
  }
  return blocks[blocks.length - 1].block_index;
}

function onFileAudioTimeUpdate() {
  if (state.codeViewMode !== "md-preview" || !state.mdPreview || !state.mdReadingChunk) return;
  // Reading one file while previewing another is legitimate (the sidebar
  // speaker icon doesn't open a preview), and a file can change on disk
  // between preview and read — in both cases the block indices refer to
  // something else, so highlight nothing rather than highlight wrongly.
  if (state.mdReadingChunk.file_path !== state.mdPreview.filePath) return;
  if (state.mdReadingChunk.content_hash !== state.mdPreview.contentHash) return;
  const duration = audioEl.duration;
  if (!Number.isFinite(duration) || duration <= 0) return; // metadata not loaded yet
  const fraction = Math.min(1, Math.max(0, audioEl.currentTime / duration));
  const index = blockAtFraction(state.mdReadingChunk.blocks, fraction);
  if (index !== state.mdReadingBlockIndex) setMdReadingBlock(index);
}

// These two do a targeted class swap rather than a re-render. That's the
// one place in this file where the DOM is updated without a full rebuild,
// and it's deliberate: timeupdate fires several times a second, and
// rebuilding at that rate wouldn't just be wasteful, it would reset the
// pane's scroll position, destroy any text selection the reviewer has made
// (which Step Into depends on) and drop focus — several times a second.
// It stays honest because it isn't a second source of truth: the state
// lives in state.mdReadingChunk/state.mdReadingBlockIndex, and renderMdPreview applies
// exactly these same classes from that state, so a full rebuild produces
// an identical result.
function setMdReadingChunk(chunk) {
  state.mdReadingChunk = chunk;
  state.mdReadingBlockIndex = null;
  if (state.codeViewMode !== "md-preview") return;
  const covered = chunk && state.mdPreview && chunk.file_path === state.mdPreview.filePath && chunk.content_hash === state.mdPreview.contentHash;
  for (const el of codeViewEl.querySelectorAll(".md-block")) {
    const index = parseInt(el.dataset.blockIndex, 10);
    el.classList.toggle("md-in-chunk", !!covered && chunkCoversBlock(chunk, index));
    el.classList.remove("md-reading");
  }
}

export function setMdReadingBlock(index) {
  state.mdReadingBlockIndex = index;
  if (state.codeViewMode !== "md-preview") return;
  let target = null;
  for (const el of codeViewEl.querySelectorAll(".md-block")) {
    const isReading = parseInt(el.dataset.blockIndex, 10) === index;
    el.classList.toggle("md-reading", isReading);
    if (isReading) target = el;
  }
  // "nearest" is a no-op when the block is already on screen, so following
  // along doesn't jerk the view on every single block change the way
  // "center" would.
  if (target && state.mdFollowReading) target.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

export function clearMdReadingHighlight() {
  state.mdReadingChunk = null;
  state.mdReadingBlockIndex = null;
  if (state.codeViewMode !== "md-preview") return;
  for (const el of codeViewEl.querySelectorAll(".md-block")) {
    el.classList.remove("md-in-chunk", "md-reading");
  }
}

// Hard-stops whatever's currently playing/queued — used both when
// switching to reading a different file (the old file's queue must not
// keep playing under the new file's "reading" icon) and, via
// stopAllAudio below, on an explicit user stop. A multi-chunk file read
// is long enough that leaving an already-buffered chunk playing would
// feel broken in a way a single narration clip's residual second
// doesn't, so this hard-stops audioEl immediately rather than just
// letting cancel_current() on the server stop new chunks from arriving.
function stopFileAudioQueue() {
  fileAudioQueue = [];
  fileAudioPlaying = false;
  // The one place the pause flag is cleared, because every path that should
  // discard a paused read reaches it: Stop and Interrupt (via
  // stopAudioForSend), navigating to another hunk, turning Speech off in
  // prefs, and starting a new read. Missing any one of them would leave the
  // NEXT read silently paused.
  fileAudioPaused = false;
  audioEl.onended = null;
  audioEl.ontimeupdate = null;
  audioEl.pause();
  audioEl.currentTime = 0;
  clearMdReadingHighlight();
}

// Guided-tour read-aloud audio (see "speak_text"/"tour_audio_chunk" in
// handlers/voice.py). Its own queue for the same reason the file reader's is
// separate from narration's (see that queue's own comment above): sharing
// would mean tour audio fighting over the transcript's speaker buttons
// (turnClips) or the read-along highlights, all meaningless for a handful
// of fixed onboarding sentences. Simpler than either of those — no replay,
// no highlight — so this mirrors
// playNextFileAudioChunk's shape, not playAudio's.
// One object rather than two bindings so the test seam at the end of this
// file can hand qa_agent something it can still mutate once this code is
// an ES module (see window.__app): an imported binding is read-only, a
// property on an imported object is not.
export const tourAudio = { queue: [], playing: false };

export function enqueueTourAudioChunk(payload) {
  tourAudio.queue.push(payload);
  if (!tourAudio.playing) playNextTourAudioChunk();
}

function playNextTourAudioChunk() {
  const next = tourAudio.queue.shift();
  if (!next) {
    tourAudio.playing = false;
    return;
  }
  tourAudio.playing = true;
  audioEl.onended = playNextTourAudioChunk;
  audioEl.ontimeupdate = null;
  audioEl.src = `data:${next.mime_type};base64,${next.audio_base64}`;
  audioEl.play().catch((err) => console.warn("audio playback blocked", err));
}

export function stopTourAudioQueue() {
  tourAudio.queue = [];
  tourAudio.playing = false;
  audioEl.onended = null;
  audioEl.ontimeupdate = null;
  audioEl.pause();
  audioEl.currentTime = 0;
}

// Full stop: the audio queue plus the "currently reading" icon state —
// deliberately separate from stopFileAudioQueue itself, since starting a
// *new* file read also needs the old queue hard-stopped but must NOT
// clear state.speakingFilePath (the caller already set it to the new file).
// All three queues, no icon/selection state — for when something else is
// about to take over the shared <audio> element and none of them should
// keep fighting over onended.
export function stopPlayback() {
  stopFileAudioQueue();
  stopNarrationQueue();
  stopTourAudioQueue();
}

export function stopAllAudio() {
  stopPlayback();
  // Unconditional: stopFileAudioQueue already cleared the highlight, and
  // the preview bar's Stop button needs dropping even when state.speakingFilePath
  // was already null (last chunk delivered but still playing).
  setSpeakingFile(null);
}

// Message types whose server-side handler calls cancel_current() before
// doing its own work (see the dispatch chain in server.py) — sending any
// of these silently cancels an in-flight speak_file read server-side with
// no notice/error of its own to tell the client it happened, so the
// "currently reading" icon state and any already-buffered audio need to
// be cleared here too rather than waiting on a message that will never
// arrive for this case. "speak_file" itself is handled separately below:
// it also cancels whatever was playing before, but must NOT clear
// state.speakingFilePath — the caller already optimistically set it to the new
// target file.
const CANCELS_CURRENT_TASK_TYPES = new Set([
  "next", "prev", "reply", "request_change", "act_now", "refine_act_now", "refresh_diff", "jump_to_hunk", "stop", "show_summary",
]);

export function stopAudioForSend(type) {
  // "audio is in flight" is deliberately broader than state.speakingFilePath:
  // that clears as soon as the last chunk is *delivered*, which can be
  // several seconds before it finishes playing. Guarding on it alone meant
  // Stop/Interrupt during that final chunk sent the message but never
  // stopped the audio already buffered on this side.
  const audioInFlight =
    !!state.speakingFilePath || fileAudioPlaying || fileAudioQueue.length > 0 || narrationPlaying ||
    narrationQueue.length > 0 || tourAudio.playing || tourAudio.queue.length > 0 || activeTurnEl !== null;
  if (type === "speak_file" || type === "speak_text" || type === "speak_turn") {
    // stopPlayback, not just the one queue this message is about to feed:
    // any of narration/file-read/tour audio still playing must stop too,
    // or two queues end up fighting over audioEl.onended. Still must NOT
    // clear state.speakingFilePath — a speak_file caller has already set it to
    // the file about to be read, and speak_text never touches it anyway.
    stopPlayback();
  } else if (audioInFlight && CANCELS_CURRENT_TASK_TYPES.has(type)) {
    stopAllAudio();
  }
  // "open_md_preview" deliberately matches neither branch, and is absent from
  // the set above: the server doesn't cancel_current for it either, so opening
  // a preview never interrupts audio that's already playing.
}
