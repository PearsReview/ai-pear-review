// The toolbar's Explain button, next to Next: explains the hunk on screen on
// request. Shown only when automatic explanations are off (the "Explain
// changes" preference in prefs.js), so a reviewer stepping through with Next
// decides hunk by hunk which ones are worth a model call. Server side, see
// handle_explain_hunk in app/handlers/narration.py.

import { state } from "./state.js";
import { send } from "./ws.js";
import { showThinking } from "./transcript.js";

const explainBtn = document.getElementById("explain-btn");

// Called whenever something it depends on changes: a new hunk on screen
// (onPresenting), its narration arriving (onNarration), a response starting
// or finishing (showThinking/clearThinking), or the preference flipping.
export function updateExplainBtn() {
  explainBtn.classList.toggle("hidden", state.autoNarrate || !state.hunkExplainable);
  // Disabled while anything is on its way, not just an explanation: the
  // request cancels whatever is in flight server-side, a reply included.
  explainBtn.disabled = state.hunkExplained || state.thinking;
  explainBtn.title = state.hunkExplained
    ? "Already explained — see the chat"
    : state.thinking
    ? "Waiting for a response…"
    : "Explain this change";
  explainBtn.setAttribute("aria-label", explainBtn.title);
}

explainBtn.addEventListener("click", () => {
  send("explain_hunk", { index: state.currentHunkIndex });
  showThinking();
});
