import { renderCodeView, showEmptyCodeView } from "./code-view.js";
import { send } from "./ws.js";
import { applyChatTabFilter, clearThinking, showThinking } from "./transcript.js";
import { clearError, reenableActionButtons } from "./status.js";
import { state } from "./state.js";
import { exitExploreAllFilesMode, renderFilteredFileList } from "./sidebar.js";
import { updateMicAvailability } from "./prefs.js";
import { renderPromptSuggestions } from "./interactions.js";
import {
  actNowConfirmRow,
  backBtn,
  backToSummaryBtn,
  codeViewEl,
  endReviewBtn,
  hunkMetaEl,
  iconHtml,
  micBtn,
  reviewAllToggleEl,
  reviewedBtn,
  sendBtn,
  startReviewBtn,
  textInput,
} from "./dom.js";
import { applyActNowAvailability, openReviewPlan, showReviewPlanRow } from "./comments.js";
import { updateExplainBtn } from "./explain.js";
import { openHandoffDialog } from "./handoff.js";
// Moving through the review: presenting a hunk, the Start/End Review
// gates, Step Into and its way back, and browsing a file that has no
// hunks at all.
//
// onPresenting is the hinge — almost every transition arrives as one of
// these payloads, and it is responsible for clearing whatever the last
// screen left behind before the new one draws.

let hasAttemptedPositionRestore = false; // only try the localStorage jump-back once per page load

// Position-only persistence across a refresh — NOT full session persistence
// (conversation history/caches still live only in server-side memory for
// the WebSocket connection's lifetime, and are lost on reconnect regardless
// of this). This just remembers which hunk you were on and jumps back to
// it once the new connection's hunk list is known, instead of silently
// sitting at hunk 0. localStorage can throw (private browsing, disabled,
// quota) — never let that break the rest of the app.
const SAVED_HUNK_INDEX_KEY = "ai_pear_review_last_hunk_index";

function saveHunkPosition(index) {
  try {
    localStorage.setItem(SAVED_HUNK_INDEX_KEY, String(index));
  } catch {
    // non-critical, ignore
  }
}

export function restoreHunkPositionIfAny(total) {
  if (hasAttemptedPositionRestore) return;
  hasAttemptedPositionRestore = true;
  let saved = null;
  try {
    saved = localStorage.getItem(SAVED_HUNK_INDEX_KEY);
  } catch {
    return;
  }
  if (saved === null) return;
  const index = parseInt(saved, 10);
  // >0 since hunk 0 is already where a fresh connection starts anyway —
  // no need to re-jump there.
  if (Number.isInteger(index) && index > 0 && index < total) {
    send("jump_to_hunk", { index });
  }
}

export function onPresenting(payload) {
  clearError();
  clearThinking();
  // Before reenableActionButtons, which reads it back to decide whether the
  // hunk navigation has anywhere to go (see updateHunkNavAvailability).
  // Every "presenting" payload carries total, including the done/no-hunks
  // ones, so this never has to fall back to a stale count.
  state.totalHunks = payload.total;
  reenableActionButtons(); // the response Next/Prev/Refresh Diff were all waiting on
  // Next/Prev always leaves step-into mode, even if it was active —
  // the reviewer moved on, the peeked-at definition is no longer relevant.
  backBtn.classList.add("hidden");
  // An open, unsent comment draft is anchored to a line that's about to
  // disappear from view — don't let it silently persist into an unrelated
  // screen (see openComposerForLine).
  state.composingComment = null;
  // A pending Act Now preview is anchored to whatever hunk/file was on
  // screen when it was generated — moving on without Apply/Discard must
  // not leave that bar sitting on top of an unrelated hunk, where it could
  // still be clicked and write stale content to the wrong place.
  actNowConfirmRow.classList.add("hidden");
  // Any real "presenting" message means the code view is about to show a
  // real hunk or the summary, not whatever was being explored — same
  // reasoning as backBtn/state.composingComment/actNowConfirmRow above.
  state.exploringFilePath = null;
  // Starting or ending the review always snaps "All files" mode back off,
  // even if the reviewer happened to be mid-explore at that moment — the
  // formal review flow takes over the sidebar/code pane from here.
  if (payload.review_started || payload.ended) exitExploreAllFilesMode();

  if (payload.done) {
    backToSummaryBtn.classList.add("hidden"); // already on the summary — nowhere for it to go
    if (payload.ended) {
      state.reviewEnded = true;
      // Reachable from any hunk, not just past the last one — see
      // send_summary_screen in web/progress.py. Narration/replies and the
      // review-mark controls are frozen for the rest of this connection,
      // whether this was reached automatically (every hunk reviewed) or
      // by pressing End Review early. Browsing away from this screen
      // (Prev/Next/a file click) still works — see the real-hunk branch
      // below — this is just what shows before that first happens.
      const headline = payload.ended_early
        ? `Review ended — ${payload.reviewed_count} / ${payload.total} hunks reviewed.`
        : `Review complete — all ${payload.total} hunks reviewed.`;
      const commentNote =
        payload.pending_comment_count > 0
          ? ` ${payload.pending_comment_count} comment(s) still queued — use Create plan to hand them off.`
          : "";
      hunkMetaEl.textContent = headline;
      showReviewPlanRow(payload.review_plan);
      showEmptyCodeView(
        iconHtml("check") +
          " " +
          headline +
          commentNote +
          (payload.review_plan ? ' <button id="summary-view-plan-btn" type="button" title="Open the last review plan in the preview">View review plan</button>' : "") +
          ' <button id="new-review-btn" type="button" title="Re-read the diff and review it again from the start">Start New Review</button>'
      );
      const summaryPlanBtn = document.getElementById("summary-view-plan-btn");
      if (summaryPlanBtn) summaryPlanBtn.addEventListener("click", openReviewPlan);
      // showEmptyCodeView just replaced #code-view's innerHTML, so the
      // button above is a fresh element each time — the listener has to
      // be (re-)attached after, not once at module load.
      document.getElementById("new-review-btn").addEventListener(
        "click",
        () => send("new_review", {}),
        { once: true }
      );
      hideStartReviewBtn();
      hideEndReviewBtn();
      setReviewControlsVisible(false);
      setComposerEnabled(false);
    } else if (!payload.total) {
      // Nothing to review at all, which is not the same thing as having
      // reviewed everything: this used to read "All hunks reviewed (0
      // total)" and claim a clean working tree had been walked through.
      // Reached when a refresh finds the diff empty (every change committed
      // or reverted mid-review — see refresh_diff in handlers/review_flow.py),
      // which is also why this points at Refresh Diff rather than at
      // reconnecting the way the on-connect error does.
      hunkMetaEl.textContent = "No changes to review.";
      showEmptyCodeView(
        iconHtml("refresh-cw") +
          " Nothing to review — no tracked file differs from the last commit. Make a change, then use Refresh Diff."
      );
    } else {
      hunkMetaEl.textContent = `All hunks reviewed (${payload.total} total).`;
      showEmptyCodeView(iconHtml("check") + " All hunks reviewed.");
    }
    state.savedHunkView = null;
    state.hunkExplainable = false;
    updateExplainBtn();
    // Also drop the last real hunk's view, not just the "Back" shortcut —
    // otherwise any unrelated re-render (a status update, a mark toggle)
    // calls rerenderCurrentView() and silently redraws that stale hunk
    // underneath the "All hunks reviewed" header.
    state.lastRenderedView = null;
    state.currentFilePath = "";
    applyChatTabFilter();
    renderPromptSuggestions();
    return;
  }
  hunkMetaEl.textContent = `Hunk ${payload.index + 1} / ${payload.total} — ${payload.file_path}`;
  state.currentFilePath = payload.file_path;
  applyChatTabFilter(); // File chat tab must re-filter to the newly active file
  renderPromptSuggestions();
  saveHunkPosition(payload.index);
  renderCodeView(payload.full_lines, payload.highlight_start, payload.highlight_end, payload.file_path);
  // Stashed so "Step Into" -> "Back" can restore this without a server
  // round-trip; the full diff content is already here in the browser.
  state.savedHunkView = {
    hunkMetaText: hunkMetaEl.textContent,
    fullLines: payload.full_lines,
    highlightStart: payload.highlight_start,
    highlightEnd: payload.highlight_end,
    filePath: payload.file_path,
  };
  // The code view is up immediately; narration (the persona's text) arrives
  // separately once briefing + the conversation call finish (see server.py
  // present_current_hunk) — show a placeholder in the meantime rather than
  // a transcript that just sits empty. Until the reviewer explicitly starts
  // the review (see handle_start_review in
  // handlers/review_flow.py), no briefing/conversation
  // call is even in flight yet — the toolbar's Start Review button (see
  // updateStartReviewBtn) is what kicks it off, not a spinner for work
  // that hasn't started.
  //
  // review_ended CAN be true here now — Prev/Next/a file click still work
  // after a review ends (present_current_hunk no longer redirects them all
  // to the summary; see that function's docstring), so this branch has to
  // handle "browsing a real hunk, but the review is over" as its own case,
  // not just "active" and "not-yet-started".
  state.reviewEnded = !!payload.review_ended;
  state.currentHunkIndex = payload.index;
  state.hunkExplained = !!payload.narrated;
  state.hunkExplainable = !!payload.narration_available && !!payload.review_started && !state.reviewEnded;
  updateExplainBtn();
  // The inline "+" add-comment button (see .line-comment-add in
  // style.css) is a review action like Mark as reviewed — locked behind
  // review_started same as before, and now ALSO locked once ended, via
  // this same class (see "#code-view.comments-locked .line-comment-add"
  // in style.css).
  // codeViewEl's own class survives renderCodeView's innerHTML rewrite
  // just below, so this doesn't need to be re-applied per re-render.
  codeViewEl.classList.toggle("comments-locked", !payload.review_started || state.reviewEnded);
  state.lastKnownReviewStarted = !!payload.review_started; // see backToHunk's restore of comments-locked
  backToSummaryBtn.classList.toggle("hidden", !state.reviewEnded);
  if (state.reviewEnded) {
    // Narration/replies and the review-mark controls are frozen for the
    // rest of this connection (same "End Review" promise as the summary
    // screen's own branch above) — browsing is the one thing that still
    // works. setComposerEnabled(false) also covers Act Now (see its own
    // docstring), and the server backs all of this with its own guards
    // (handle_reply/handle_act_now/handle_confirm_act_now) rather than
    // trusting this client state alone.
    hideStartReviewBtn();
    hideEndReviewBtn();
    setReviewControlsVisible(false);
    setComposerEnabled(false);
  } else {
    // A real hunk is now on screen, so there's something to reply to
    // regardless of review_started — see setComposerEnabled's docstring.
    setComposerEnabled(true);
    if (payload.narration_available && !payload.review_started) {
      updateStartReviewBtn();
      hideEndReviewBtn();
      setReviewControlsVisible(false);
    } else {
      hideStartReviewBtn();
      setReviewControlsVisible(true);
      if (payload.review_started) updateEndReviewBtn();
      else hideEndReviewBtn();
      // Only when narration is actually on its way: with automatic
      // explanations off, an unexplained hunk waits for the Explain button.
      if (payload.narrating ?? payload.narration_available) showThinking();
    }
  }
}

// Arrives once the conversation agent (and, before it, the briefing agent)
// finish — separate from "presenting" specifically so the code view doesn't
// sit blank while those run.
//
// The server still sends this on every revisit of a hunk (Prev, jump_to_hunk,
// landing back on one via Next) — it's what clears showThinking() below, so
// it can't just be skipped — but it replays the exact same text rather than
// generating something new (see present_current_hunk in
// handlers/narration.py). Track
// what's already been shown per hunk index so a revisit doesn't also add a
// duplicate bubble to the transcript. Keyed off the text itself, not just the
// index, so a hunk index reused for new content after a refresh_diff is
// never mistaken for a replay.

// the very first "presenting" already carries review_started: true.
function updateStartReviewBtn() {
  startReviewBtn.classList.remove("hidden");
  // Composer stays enabled here — see setComposerEnabled's docstring. This
  // button only gates the automatic narration call, not replying.
}

function hideStartReviewBtn() {
  startReviewBtn.classList.add("hidden");
}

// Shown once the review is live (review_started, not yet review_ended) —
// gated on review_started alone, not narration_available, so it stays
// available even in degraded mode if a persisted review was already
// started before the conversation agent became unreachable.
function updateEndReviewBtn() {
  endReviewBtn.classList.remove("hidden");
}

function hideEndReviewBtn() {
  endReviewBtn.classList.add("hidden");
}

// "Mark as reviewed"/"Review all" act on the current hunk/whole walkthrough
// respectively — neither means anything before the reviewer has actually
// started looking at narrated content, so they stay hidden alongside the
// Start Review button and appear together with it disappearing. Also
// re-hidden (permanently, for this connection) once the review ends — see
// onPresenting's payload.ended branch.
function setReviewControlsVisible(visible) {
  reviewedBtn.classList.toggle("hidden", !visible);
  reviewAllToggleEl.classList.toggle("hidden", !visible);
}

// Disappears once clicked — there is nothing left to start for the rest of
// this connection (review_started never resets back to false server-side;
// see app/server.py's Session).
startReviewBtn.addEventListener("click", () => {
  hideStartReviewBtn();
  send("start_review", {});
});

// Disappears once clicked, same as Start — review_ended never resets back
// either. With comments still queued it first offers to create the plan
// from them (the dialog then ends the review itself).
endReviewBtn.addEventListener("click", () => {
  if (state.queuedComments.length > 0) {
    openHandoffDialog({ fromEndReview: true });
    return;
  }
  hideEndReviewBtn();
  send("end_review", {});
});

// Gated on there being a current hunk (or an explored file) to talk about at
// all — not on review_started. handle_reply server-side never required
// review_started either (it just falls back to an empty conversation history
// if narration hasn't run yet for this hunk), and typing+sending is already
// an explicit opt-in the same way clicking Start Review is, so there's no
// reason to make the reviewer click Start Review first just to ask a
// question. Start Review still gates the *automatic* per-hunk narration
// call and comment queuing/marking (see comments-locked in style.css) —
// only the reply composer itself is exempt.
// micBtn re-enabling defers to updateMicAvailability() rather than being set
// here directly, so this never overrides its own state.sttAvailable/state.sttPrefOn
// gating (see updateMicAvailability).
export function setComposerEnabled(enabled) {
  textInput.disabled = !enabled;
  sendBtn.disabled = !enabled;
  applyActNowAvailability();
  if (enabled) {
    updateMicAvailability();
  } else {
    micBtn.disabled = true;
  }
}

// "Back to hunk" is only honest when there is a hunk to go back to. With
// no hunks in the session at all (a repo with nothing to review, where the
// only thing ever on screen is an explored file or a preview) the button
// pointed nowhere: backToHunk's own no-savedHunkView branch left the
// explored file sitting there and the button still showing, so it read as
// broken. Hidden rather than disabled, matching how every other caller
// already controls it. Shown from the three views that are a detour from
// the walkthrough — Step Into, explore mode, and the markdown preview —
// which is why this lives here rather than being inlined at each.
export function showBackToHunkBtn() {
  backBtn.classList.toggle("hidden", state.totalHunks === 0);
}

// "Step Into" response — a peek at wherever the selected identifier is
// declared, which may be a file this hunk's diff never touched. Not a
// walkthrough position change: session.index/hunks are untouched
// server-side, this is purely a frontend view swap with a way back.
export function onDefinition(payload) {
  clearError();
  reenableActionButtons(); // the response step-into-btn was waiting on
  state.composingComment = null; // see onPresenting — a Step Into peek is a view swap too
  actNowConfirmRow.classList.add("hidden"); // see onPresenting — same stale-preview hazard
  const lines = payload.lines.map((text, i) => ({
    kind: "context",
    old_lineno: null,
    new_lineno: payload.context_start + i,
    text,
  }));
  const targetIndex = payload.line_number - payload.context_start;
  hunkMetaEl.textContent = `Definition of "${payload.identifier}" — ${payload.file_path}:${payload.line_number}`;
  renderCodeView(lines, targetIndex, targetIndex, payload.file_path);
  showBackToHunkBtn();
}

// "All files" explore mode — opening a file from the sidebar that has no
// diff hunks (see handle_explore_file in
// handlers/explore.py). Same view-swap-with-
// a-way-back mechanism as onDefinition above (Step Into), just over the
// whole file with nothing highlighted, since nothing in it changed.
export function onFileExplore(payload) {
  clearError();
  state.composingComment = null;
  actNowConfirmRow.classList.add("hidden");
  const lines = payload.lines.map((text, i) => ({
    kind: "context",
    old_lineno: null,
    new_lineno: i + 1,
    text,
  }));
  hunkMetaEl.textContent = `Exploring ${payload.file_path} (unchanged)`;
  renderCodeView(lines, -1, -1, payload.file_path);
  showBackToHunkBtn();
  state.currentFilePath = payload.file_path; // so File chat filters to this file's Q&A
  state.exploringFilePath = payload.file_path;
  // The "+" add-comment button has no hunk to anchor a comment to on a
  // file outside session.hunks entirely — handle_request_change would
  // fall back to whatever hunk happens to be "current" server-side and
  // misattribute the comment to it. Locked here regardless of
  // review_started/comments-locked's usual hunk-driven state (restored in
  // backToHunk).
  codeViewEl.classList.add("comments-locked");
  // Explore-mode conversation always works, same as a real hunk's reply
  // composer now does — see setComposerEnabled's docstring.
  setComposerEnabled(true);
  applyChatTabFilter();
  renderPromptSuggestions();
  // state.currentFilePath just changed but no review_progress message is coming
  // to trigger the usual re-render (explored files aren't part of the
  // diff) — refresh the sidebar ourselves so the active-pill highlight
  // follows the reviewer here, same reasoning as onMdPreview/setSpeakingFile.
  renderFilteredFileList();
}

// Batched review workflow: a review comment no longer sends anything
// immediately — it queues (see server.py's handle_request_change), shown
// as an inline badge/card anchored to its line in the code view (see
// buildLineCommentRowHtml) until "Create plan" builds and hands off
// everything queued at once. state.queuedComments is the client-side mirror
// renderMergedCodeView/renderSplitCodeView read from on every render.
