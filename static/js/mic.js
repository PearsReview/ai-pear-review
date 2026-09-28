// Push-to-talk recording: hold the mic button (or Space/Enter while it has
// focus) to record, release to transcribe.
//
// One shared MediaRecorder for the whole page, because only one recording
// can be in flight at a time and two recorders would fight over the same
// microphone. Both entry points route through it: the composer's mic button
// at the bottom, and the per-line mic inside an inline comment composer —
// which is why startRecording takes the button that triggered it, so the
// recording indicator lands on the right control.

import { state } from "./state.js";
import { send } from "./ws.js";
import { showThinking } from "./transcript.js";
import { setActNowActive } from "./comments.js";
import { showError } from "./status.js";
import { codeViewEl, iconHtml, micBtn } from "./dom.js";
import { buildMarkedContext, clearMarkedLines, submitComposerAudio } from "./interactions.js";

// --- Push-to-talk mic — a single shared recorder, since there's only one
// physical microphone. Both the bottom #mic-btn (plain reply / Act Now)
// and each inline comment composer's own mic button (request_change) share
// this same getUserMedia/MediaRecorder singleton via a swappable
// completion callback, rather than each keeping its own recording logic. ---
let mediaRecorder = null;
let audioChunks = [];
// True between mousedown and mouseup — lets startRecording() notice if the
// button was already released by the time getUserMedia() (async) resolves,
// so a very quick click can't leave a recording running with nothing left
// to ever call .stop() on it (mouseup already fired and found
// mediaRecorder still null at that point).
let recordingRequested = false;
let currentRecordingOnDone = null; // (base64Audio) => void — set by whichever caller starts the recording
let currentRecordingBtn = micBtn; // whichever button triggered this recording — gets the ".recording" visual cue

export async function startRecording(onDone, triggerBtn = micBtn) {
  if (!state.sttAvailable || !state.sttPrefOn) return;
  currentRecordingOnDone = onDone;
  currentRecordingBtn = triggerBtn;
  recordingRequested = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (!recordingRequested) {
      stream.getTracks().forEach((t) => t.stop());
      showError("Recording too short — try holding the mic button a little longer.");
      return;
    }
    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => audioChunks.push(e.data);
    mediaRecorder.onstop = onRecordingStop;
    mediaRecorder.start();
    currentRecordingBtn.classList.add("recording");
  } catch (err) {
    showError(`Microphone unavailable: ${err.message}. Use the text box instead.`);
  }
}

export function stopRecording() {
  recordingRequested = false;
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
  // The composer's mic button is rebuilt on every render (a fresh DOM node
  // each time) — if a re-render happened mid-recording, this reference may
  // already be detached, in which case removing its class is a harmless
  // no-op rather than an error.
  currentRecordingBtn.classList.remove("recording");
}

// Pure recording mechanics + dispatch to whichever completion callback
// startRecording() was given — no knowledge here of Act Now/plain reply/
// inline review comment; that all lives in the callbacks themselves
// (onBottomMicDone, submitComposerAudio).
async function onRecordingStop() {
  const onDone = currentRecordingOnDone;
  currentRecordingOnDone = null;
  if (!audioChunks.length) {
    // Shouldn't normally happen now that the race above is closed, but
    // cheap to guard directly rather than send an empty payload and make
    // the server say "Empty reply." for what's really a client-side issue.
    showError("No audio captured — try holding the mic button a little longer.");
    return;
  }
  const blob = new Blob(audioChunks, { type: "audio/webm" });
  const buffer = await blob.arrayBuffer();
  const base64 = btoa(String.fromCharCode(...new Uint8Array(buffer)));
  if (onDone) onDone(base64);
}

export function onBottomMicDone(base64) {
  if (state.exploringFilePath) {
    // Same reasoning as sendTextReply's identical early return.
    send("explore_reply", { audio_base64: base64, file_path: state.exploringFilePath });
    showThinking();
    return;
  }
  const markedContext = buildMarkedContext();
  const payload = markedContext ? { audio_base64: base64, marked_lines: markedContext } : { audio_base64: base64 };
  if (state.actNowActive) {
    send("act_now", payload);
    state.actNowPending = true;
    setActNowActive(false);
    showThinking(
      iconHtml("zap") + ` ${state.actNowStatus.agent || "the agent"} is working on it — this can take a minute...`,
      "act-now-pending"
    );
  } else {
    // Unlike the typed-reply path (sendTextReply), voice genuinely has a
    // real wait here — STT transcription happens server-side before
    // "human_turn" (and its own showThinking() call) ever arrives, so
    // without this the reviewer sees nothing at all between releasing the
    // mic button and the transcript appearing. onHumanTurn's later
    // showThinking() call just refreshes the same bubble once the actual
    // reply starts generating — harmless, showThinking() is idempotent.
    send("reply", payload);
    showThinking();
  }
  if (markedContext) clearMarkedLines();
}

micBtn.addEventListener("mousedown", () => startRecording(onBottomMicDone));
micBtn.addEventListener("mouseup", stopRecording);
micBtn.addEventListener("mouseleave", stopRecording);
micBtn.addEventListener("touchstart", (e) => { e.preventDefault(); startRecording(onBottomMicDone); });
micBtn.addEventListener("touchend", (e) => { e.preventDefault(); stopRecording(); });
// If the OS/browser cancels the touch mid-gesture (e.g. a system gesture
// interrupts it), touchend never fires — without this, the button could
// be left stuck showing ".recording" with no way to clear it.
micBtn.addEventListener("touchcancel", stopRecording);
// Keyboard equivalent of the mousedown/mouseup push-to-talk pair above —
// this button advertises aria-label="Push to talk" and is a real tab stop,
// but had no keydown/keyup handling at all, so Space/Enter did nothing.
// preventDefault on keydown stops Space from also scrolling the page and
// suppresses the synthetic click a <button> fires on release, which would
// otherwise fight with the press/release model here. e.repeat is guarded
// so an OS key-repeat doesn't call startRecording() again while already
// recording (mousedown has no equivalent, since a held mouse button
// doesn't re-fire). blur mirrors mouseleave: if focus moves away while
// held (e.g. Tab, or a screen reader command), stop rather than leave the
// recording stuck on.
micBtn.addEventListener("keydown", (e) => {
  if ((e.key === " " || e.key === "Enter") && !e.repeat) {
    e.preventDefault();
    startRecording(onBottomMicDone);
  }
});
micBtn.addEventListener("keyup", (e) => {
  if (e.key === " " || e.key === "Enter") {
    e.preventDefault();
    stopRecording();
  }
});
micBtn.addEventListener("blur", stopRecording);

// Inline comment composer's own mic button — rebuilt on every render, so
// (like the click delegation above) this is bound once on #code-view via
// delegation rather than per-element. mouseup/touchend are bound on
// document rather than the button itself since a press-drag off a tiny
// inline button has no per-element mouseleave equivalent under
// delegation; stopRecording() is idempotent, so this is harmless
// alongside the #mic-btn-specific bindings above.
codeViewEl.addEventListener("mousedown", (e) => {
  const btn = e.target.closest(".line-comment-mic-btn");
  if (btn) startRecording(submitComposerAudio, btn);
});
codeViewEl.addEventListener("touchstart", (e) => {
  const btn = e.target.closest(".line-comment-mic-btn");
  if (btn) { e.preventDefault(); startRecording(submitComposerAudio, btn); }
});
document.addEventListener("mouseup", stopRecording);
document.addEventListener("touchend", stopRecording);
document.addEventListener("touchcancel", stopRecording);
// Keyboard equivalent for the composer's own mic button — same rationale
// as #mic-btn's keydown/keyup pair above, delegated the same way its
// mousedown/touchstart handling already is (this button is rebuilt on
// every render, so binding per-element would leak/re-bind constantly).
// document-level keyup (rather than per-button) matches the existing
// document-level mouseup/touchend below, for the same reason noted there.
codeViewEl.addEventListener("keydown", (e) => {
  const btn = e.target.closest(".line-comment-mic-btn");
  if (btn && (e.key === " " || e.key === "Enter") && !e.repeat) {
    e.preventDefault();
    startRecording(submitComposerAudio, btn);
  }
});
document.addEventListener("keyup", (e) => {
  if (e.key === " " || e.key === "Enter") stopRecording();
});
