// Entry point. Importing a module is what installs its listeners, so the
// import list below is also the list of features that exist.
//
// Everything that needs to happen once, at startup, happens here rather than
// at the top level of a feature module: a module that did real work while
// being evaluated could run before a module it depends on had finished
// evaluating, which is what turns an ordinary import cycle into a crash.

import { state } from "./state.js";
import { setDiffViewMode } from "./prefs.js";
import { send } from "./ws.js";
import { backToHunk, setActNowActive } from "./comments.js";
import {
  actNowBtn,
  backBtn,
  codeViewEl,
  stepIntoBtn,
} from "./dom.js";
import { tourVisibleStepIndices } from "./tour.js";
import {
  blockAtFraction,
  enqueueFileAudioChunk,
  fileAudioPaused,
  locateSentences,
  playAudio,
  playTurnAudio,
  tourAudio,
} from "./audio.js";
import { appendTurn, onNarration } from "./transcript.js";
import { onMdPreview } from "./md-preview.js";
import { onPresenting } from "./review-flow.js";
import { renderCodeView, rerenderCurrentView } from "./code-view.js";
import { updateReviewProgress, updateStatus } from "./status.js";

import "./prefs.js";
import "./settings.js";
import "./sidebar.js";
import "./interactions.js";
import "./mic.js";

actNowBtn.addEventListener("click", () => setActNowActive(!state.actNowActive));

// Selecting a function/class/variable name in the code view enables
// "Step Into" — sent as-is to the server, which does the actual lookup
// (see code_search.py); nothing here validates it's a real identifier.
codeViewEl.addEventListener("mouseup", () => {
  state.currentSelection = (window.getSelection()?.toString() || "").trim();
  stepIntoBtn.disabled = !state.currentSelection;
});
stepIntoBtn.addEventListener("click", () => {
  if (!state.currentSelection) return;
  stepIntoBtn.disabled = true; // re-enabled by reenableActionButtons() once the resulting "definition" (or an error) arrives
  send("step_into", { text: state.currentSelection });
});
backBtn.addEventListener("click", backToHunk);

// Deliberate test seam for qa_agent. Everything this exposes is either a
// pure helper fed synthetic input (blockAtFraction), a handler with no
// click that reaches it (onPresenting's done:true summary state), or a
// service flag no interaction can force (state.ttsAvailable). Once this
// file is split into ES modules its top-level names stop being globals,
// and page.evaluate has no other way in.
//
// This is the only thing in this file that exists for the tests. Reach for
// a real interaction first; add to this object only when there isn't one.
window.__app = {
  state,
  tourAudio,
  send,
  sendHook: null,
  onPresenting,
  onNarration, // a real narration needs a live model
  onMdPreview,
  updateStatus,
  updateReviewProgress,
  renderCodeView,
  rerenderCurrentView,
  setDiffViewMode,
  blockAtFraction,
  tourVisibleStepIndices,
  // No real interaction reaches the read-aloud pause controls: starting a
  // file read needs a live TTS service, which no test environment here
  // runs (see the note at the top of qa_agent/test_md_preview.py). Feeding
  // a synthetic file_audio_chunk is the only way in. The clicking itself
  // is still done for real, on the real buttons.
  enqueueFileAudioChunk,
  // Same reason, for the chat read-along highlight: a presenter turn needs
  // a live model and its audio a live TTS service, so both are fed in.
  appendTurn,
  playAudio,
  playTurnAudio,
  locateSentences, // pure: the sentence matching, fed real server output
  get fileAudioPaused() {
    return fileAudioPaused; // a getter, not a copy — the import is a live binding, the property would be a snapshot
  },
};
