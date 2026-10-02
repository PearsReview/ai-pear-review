
import { maybeAutoStartTour, onTourTtsUnavailable } from "./tour.js";
import { clearThinking } from "./transcript.js";
import { restoreHunkPositionIfAny, setComposerEnabled } from "./review-flow.js";
import { applyActNowAvailability, endActNowRefine, setActNowActive, updateReviewQueueUI } from "./comments.js";
import { state } from "./state.js";
import { renderFileList } from "./sidebar.js";
import { updateMicAvailability } from "./prefs.js";
import { setSpeakingFile } from "./md-preview.js";
import {
  actNowConfirmRow,
  errorBannerEl,
  makeIconSpan,
  nextBtn,
  prevBtn,
  refreshDiffBtn,
  reviewAllIcon,
  reviewAllToggleEl,
  reviewProgressEl,
  reviewedBtn,
  reviewedBtnIcon,
  reviewedBtnLabel,
  statusBarEl,
  stepIntoBtn,
} from "./dom.js";
import { rerenderCurrentView } from "./code-view.js";
import { clearMdReadingHighlight, onTurnAudioUnavailable } from "./audio.js";

let llmInputTokens = 0;
let llmOutputTokens = 0;
// Status bar, error and notice banners, and the review-progress line.
//
// showError is the app's unwinder: a failed request can leave an inline
// comment mid-submit, an Act Now armed, or a file half-read, so it clears
// all three rather than only showing text. reenableActionButtons exists for
// the same reason — every button disabled while in flight gets restored
// here, not only on the happy path.

// Prev/Next step through session.hunks, so with no hunks at all they have
// nowhere to go. Left live, they weren't merely inert: clicking one still
// sent "next"/"prev", and the server's reply (present_current_hunk's
// current_hunk-is-None branch) painted "All hunks reviewed (0 total)" over
// the "No changes found to review" error — telling a reviewer with an
// untouched working tree that they'd finished reviewing it. Disabled rather
// than hidden, so the toolbar keeps its shape and the tooltip says why.
export function updateHunkNavAvailability() {
  const nothingToReview = state.totalHunks === 0;
  nextBtn.disabled = nothingToReview;
  prevBtn.disabled = nothingToReview;
  const none = "Nothing to review — this repo has no changed hunks";
  nextBtn.title = nothingToReview ? none : "Next hunk";
  prevBtn.title = nothingToReview ? none : "Previous hunk";
}

export function reenableActionButtons() {
  updateHunkNavAvailability(); // restores the hunk-count gating, not just "enabled"
  refreshDiffBtn.disabled = false;
  stepIntoBtn.disabled = !state.currentSelection; // restores the selection-driven gating, not just "enabled"
  updateReviewQueueUI(state.queuedComments.length); // restores finishReviewBtn's own count-based gating
  // Also restores the reply composer to whatever state it should actually
  // be in right now, not whatever a stale disable left it at. Without
  // this, a narration/reply call that fails (a slow or errored local LLM,
  // in particular) left the composer disabled forever: the only other
  // place that ever re-enables it is onPresenting, which never runs again
  // if the call that failed wasn't itself triggered by a new hunk landing.
  // showError already calls this function, which is what actually fixes
  // "the chat input won't accept text" after any such failure. Enabled
  // whenever there's a real hunk (state.savedHunkView) or an explored file to
  // talk about — not gated on review_started, see setComposerEnabled.
  setComposerEnabled(!!state.savedHunkView || !!state.exploringFilePath);
}

export function showError(message) {
  clearThinking();
  reenableActionButtons();
  // A failed speak_file read never sends the final file_audio_chunk that
  // would otherwise clear this (see enqueueFileAudioChunk) — any error
  // arriving while a file is marked as reading is treated as that read
  // having ended, since nothing else that can still error out mid-read
  // continues running alongside it in this app's one-thing-in-flight model.
  if (state.speakingFilePath) {
    clearMdReadingHighlight();
    setSpeakingFile(null);
  }
  // An error means whatever Act Now preview is on screen (if any) is now
  // unconfirmed and unexplained — same stale-preview hazard as onPresenting.
  // Except a failed refine: the preview it started from is still the pending
  // one server-side, so the bar comes back as it was.
  if (state.actNowRefining) endActNowRefine();
  else actNowConfirmRow.classList.add("hidden");
  if (state.actNowPending) {
    // The in-flight request that just failed was an Act Now, and the
    // reviewer never got a preview to Apply/Discard — re-activate the
    // button so simply retrying the same instruction goes out as another
    // Act Now instead of silently becoming a plain chat reply (it was
    // already deactivated, one-shot, the moment the failed request was sent).
    state.actNowPending = false;
    setActNowActive(true);
  }
  if (state.composingComment && state.composingComment.submitting) {
    // The composer's own request_change failed — re-enable Send/mic rather
    // than leaving them disabled forever (see submitComposerText/Audio):
    // the composer stays open with the reviewer's text/anchor intact so
    // they can just retry, instead of having lost the draft already.
    state.composingComment.submitting = false;
    rerenderCurrentView();
  }
  errorBannerEl.textContent = message;
  errorBannerEl.classList.remove("hidden", "notice-success", "notice-info");
}

// "notice" reuses the same banner as errors — level just picks the color,
// since an applied-change confirmation shouldn't read as a failure.
export function showNotice(message, level, { clearPending = true } = {}) {
  // A notice (e.g. Act Now's "no change was proposed") is itself a
  // definitive outcome for whatever was in flight — clear the "…thinking"/
  // "…generating" bubble the same way showError does, or it sits there
  // indefinitely since no presenting/narration/error frame is coming to
  // replace it. clearPending: false is for a notice about something that
  // is no longer in flight (onActNowStopped), whose bubble may be another
  // action's by now.
  if (clearPending) clearThinking();
  errorBannerEl.textContent = message;
  errorBannerEl.classList.remove("hidden", "notice-success", "notice-info");
  if (level === "success") errorBannerEl.classList.add("notice-success");
  else if (level === "info") errorBannerEl.classList.add("notice-info");
}

export function clearError() {
  errorBannerEl.classList.add("hidden");
  errorBannerEl.classList.remove("notice-success", "notice-info");
  errorBannerEl.textContent = "";
}

export function updateStatus(payload) {
  if (typeof payload.stt === "boolean") state.sttAvailable = payload.stt;
  if (typeof payload.tts === "boolean") {
    state.ttsAvailable = payload.tts;
    // If the tour's own speech toggle was on when the service was found
    // to be down, turn it off rather than leaving the button showing
    // "on" for something that — per speakTourStep's own guard — will not
    // actually speak again until state.ttsAvailable is true. Defined near the
    // end of the file (with the rest of the tour code) but safe to call
    // from here: it's a plain function declaration, hoisted, and this
    // only ever runs later, once a real WS message has arrived.
    if (!state.ttsAvailable) {
      onTourTtsUnavailable();
      onTurnAudioUnavailable();
    }
  }
  if (typeof payload.llm === "boolean") state.llmAvailable = payload.llm;
  if (typeof payload.briefing === "boolean") state.briefingAvailable = payload.briefing;
  if (typeof payload.llm_input_tokens === "number") llmInputTokens = payload.llm_input_tokens;
  if (typeof payload.llm_output_tokens === "number") llmOutputTokens = payload.llm_output_tokens;
  if (payload.act_now) {
    state.actNowStatus = payload.act_now;
    applyActNowAvailability();
  }

  updateMicAvailability();

  statusBarEl.innerHTML = "";
  // Briefing "down" isn't fatal (present_hunk() falls back to the raw diff
  // with no extra context) unlike LLM/STT/TTS, but still worth surfacing —
  // it explains why presentations might feel thinner than usual.
  for (const [label, ok] of [
    ["LLM", state.llmAvailable],
    ["Briefing", state.briefingAvailable],
    ["STT", state.sttAvailable],
    ["TTS", state.ttsAvailable],
  ]) {
    const span = document.createElement("span");
    if (ok) {
      span.textContent = `${label}: ok`;
    } else {
      // Down state pairs an icon with the text — every other status
      // affordance in the app does the same, color alone isn't enough.
      span.classList.add("down");
      span.append(makeIconSpan("x"), document.createTextNode(` ${label}: down`));
    }
    statusBarEl.appendChild(span);
  }

  // No dollar figure here on purpose — local inference has no per-token
  // price, and it wouldn't be accurate for the cloud provider either
  // without hardcoding pricing that can change. Raw counts only.
  const tokensSpan = document.createElement("span");
  tokensSpan.textContent = `Tokens: ${llmInputTokens.toLocaleString()} in / ${llmOutputTokens.toLocaleString()} out`;
  statusBarEl.appendChild(tokensSpan);
}

// Reviewed status is explicit (a toggle the reviewer presses), not inferred
// from having opened a hunk — you might look at one, discuss it, and only
// mark it once you're actually satisfied. review_progress arrives after
// every hunk transition and every toggle, so this is always in sync with
// the currently-displayed hunk without the frontend tracking its own copy
// of which indices are reviewed.
export function updateReviewProgress(payload) {
  // Redundant with onPresenting's own assignment on most transitions (the
  // server always sends "presenting" first — see present_current_hunk/
  // send_summary_screen), but review_progress can also arrive alone (a
  // plain toggle_reviewed), so this keeps state.reviewEnded accurate either way
  // rather than depending on message order.
  state.reviewEnded = !!payload.review_ended;
  // Same reasoning, same pair of senders — see state.totalHunks. This is the
  // message that carries it even when no "presenting" ever arrives, which is
  // exactly the no-changes case (websocket_endpoint sends review_progress
  // and then an error instead of presenting anything).
  state.totalHunks = payload.total;
  updateHunkNavAvailability();
  reviewProgressEl.textContent = `Reviewed: ${payload.reviewed_count} / ${payload.total}`;
  const isReviewed = !!payload.current_reviewed;
  reviewedBtnIcon.setAttribute("href", isReviewed ? "#icon-square-check" : "#icon-square");
  reviewedBtnLabel.textContent = isReviewed ? "Reviewed" : "Mark as reviewed";
  reviewedBtn.classList.toggle("reviewed", isReviewed);
  reviewedBtn.setAttribute("aria-pressed", String(isReviewed));
  reviewedBtn.title = isReviewed ? "Unmark this hunk as reviewed" : "Mark this hunk as reviewed";
  // Pressed only once every hunk in every file is reviewed — reflects
  // actual state rather than remembering its own click, same reasoning as
  // reviewedBtn above (review_progress is the single source of truth).
  // Clicking it then clears them all (handle_toggle_reviewed_all).
  const allReviewed = payload.total > 0 && payload.reviewed_count === payload.total;
  reviewAllIcon.setAttribute("href", allReviewed ? "#icon-list-checked" : "#icon-list-boxes");
  reviewAllToggleEl.classList.toggle("reviewed", allReviewed);
  reviewAllToggleEl.setAttribute("aria-pressed", String(allReviewed));
  reviewAllToggleEl.title = allReviewed
    ? "Unmark every hunk in every file"
    : "Mark every hunk in every file as reviewed";
  renderFileList(payload.files || [], !!payload.review_started);
  // First review_progress after connecting is the earliest point the total
  // hunk count is known — try jumping back to where a previous session
  // left off, if any (see saveHunkPosition/restoreHunkPositionIfAny).
  restoreHunkPositionIfAny(payload.total);
  // Same reasoning: the earliest point it's known there's actually
  // something to show a first-time reviewer around (see maybeAutoStartTour,
  // defined further down — function declarations are hoisted, so calling
  // it here ahead of its own definition is fine).
  maybeAutoStartTour(payload.total);
}
