
import { send } from "./ws.js";
import {
  applyChatTabFilter,
  applyLookDeeperAvailability,
  clearThinking,
  clearThinkingIf,
  showThinking,
} from "./transcript.js";
import { clearError, showNotice } from "./status.js";
import { state } from "./state.js";
import { renderFilteredFileList } from "./sidebar.js";
import { setComposerEnabled } from "./review-flow.js";
import { renderPromptSuggestions } from "./interactions.js";
import {
  actNowApplyBtn,
  actNowBtn,
  actNowConfirmRow,
  actNowDiscardBtn,
  actNowFilesEl,
  actNowRefineBtn,
  actNowRefineInput,
  actNowSummaryEl,
  backBtn,
  codeViewEl,
  finishReviewBtn,
  hunkMetaEl,
  iconHtml,
  makeIconSpan,
  reviewQueueCount,
  reviewQueueRow,
  textInput,
  viewPlanBtn,
} from "./dom.js";
import {
  DEFAULT_COMMENT_SEVERITY,
  renderCodeView,
  rerenderCurrentView,
  showEmptyCodeView,
} from "./code-view.js";
// Queued inline review comments and the Act Now preview/apply step.
//
// The queue mirrors session.pending_review_comments server-side; every
// change arrives as its own message rather than a whole-list resync, so
// the badge counts stay right without re-rendering the diff.
//
// Act Now lives here because its confirm row occupies the same strip
// above the code view and unwinds through the same paths.

export function onReviewCommentQueued(payload) {
  clearThinking();
  // This is the ack for whatever composer submission is in flight (only
  // one composer can be open at a time) — now safe to actually close it.
  // See submitComposerText/submitComposerAudio for why this is deferred
  // to here instead of happening at send time.
  state.composingComment = null;
  state.queuedComments.push({
    id: payload.id,
    file_path: payload.file_path,
    where: payload.where,
    instruction: payload.instruction,
    severity: payload.severity || DEFAULT_COMMENT_SEVERITY,
    anchor: payload.anchor,
  });
  updateReviewQueueUI(payload.pending_count);
  rerenderCurrentView();
}

// Sent once, right after connect (see server.py's websocket_endpoint) —
// always empty today since every connection gets a brand-new Session, but
// this is the one hydration path the client relies on rather than
// assuming "nothing queued" on connect (see the wire-format docstring).
export function onReviewCommentsSync(payload) {
  state.queuedComments = payload.comments || [];
  updateReviewQueueUI(state.queuedComments.length);
  rerenderCurrentView();
}

export function onReviewCommentUpdated(payload) {
  const c = state.queuedComments.find((c) => c.id === payload.id);
  if (c) {
    c.instruction = payload.instruction;
    if (payload.severity) c.severity = payload.severity;
  }
  if (state.editingCommentId === payload.id) state.editingCommentId = null;
  rerenderCurrentView();
}

export function onReviewCommentRemoved(payload) {
  state.queuedComments = state.queuedComments.filter((c) => c.id !== payload.id);
  state.expandedCommentIds.delete(payload.id);
  if (state.editingCommentId === payload.id) state.editingCommentId = null;
  updateReviewQueueUI(payload.pending_count);
  rerenderCurrentView();
}

// The plan is written (no LLM call happened server-side): clear the queued
// badges and cards from the code view, say what to give the agent, and open
// the plan. Nothing is copied unasked — the plan preview's Copy instruction
// and Copy plan buttons do that (see updateMdCopyButtons in md-preview.js).
export function onReviewFinished(payload) {
  state.queuedComments = [];
  state.expandedCommentIds.clear();
  state.editingCommentId = null;
  updateReviewQueueUI(0);
  rerenderCurrentView();
  state.handoff = { planFile: payload.plan_file, instruction: payload.instruction_line };
  const count = `${payload.comment_count} comment${payload.comment_count === 1 ? "" : "s"}`;
  let message = `Plan created from ${count}, saved to ${payload.plan_file}. `;
  message += payload.skill_written
    ? "In Claude Code or Cline, run /apply-review or ask it to apply the review."
    : `Give your coding agent: ${payload.instruction_line}`;
  if (payload.skill_note) message += ` (${payload.skill_note})`;
  // Shown by onMdPreview once the plan opens below — showing it now would be
  // wiped straight away by that preview's clearError.
  state.noticeAfterPreview = { message, level: payload.skill_note ? "info" : "success" };
  showReviewPlanRow(payload.plan_file);
  openReviewPlan();
}

export function showReviewPlanRow(planFile) {
  state.lastReviewPlan = planFile || null;
  viewPlanBtn.classList.toggle("hidden", !state.lastReviewPlan);
}

export function openReviewPlan() {
  if (state.lastReviewPlan) send("open_md_preview", { file_path: state.lastReviewPlan });
}

viewPlanBtn.addEventListener("click", openReviewPlan);

export function updateReviewQueueUI(count) {
  reviewQueueRow.classList.toggle("hidden", count === 0);
  const comments = `${count} comment${count === 1 ? "" : "s"}`;
  reviewQueueCount.replaceChildren(makeIconSpan("message-square"), document.createTextNode(` ${comments}`));
  reviewQueueCount.title = `${comments} waiting to go into the plan`;
  finishReviewBtn.disabled = count === 0;
}

// Act Now: the server only *proposes* a change here — nothing is written
// to disk until the reviewer explicitly confirms (see
// actNowApplyBtn/actNowDiscardBtn below and server.py's
// handle_act_now/handle_confirm_act_now split). Reuses renderCodeView, the
// same rendering the normal hunk view and Step Into already use, so the
// proposed change gets the full merged/split-view treatment for free.
export function onActNowPreview(payload) {
  clearThinking();
  clearError();
  state.actNowPending = false; // succeeded — no need to re-activate Act Now on a later unrelated error
  const files = payload.files && payload.files.length ? payload.files : [payload];
  const count = files.length === 1 ? "1 file" : `${files.length} files`;
  document.getElementById("act-now-confirm-label").textContent =
    `${payload.agent || "The agent"} proposes changes to ${count} — review before applying`;
  actNowSummaryEl.textContent = payload.summary || "";
  // One button per file when there's more than one: Apply writes all of
  // them together, so each must be viewable before that.
  actNowFilesEl.replaceChildren();
  if (files.length > 1) {
    files.forEach((file, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = `${file.file_path}${file.status && file.status !== "modified" ? ` (${file.status})` : ""}`;
      btn.title = `Show the proposed change to ${file.file_path}`;
      btn.addEventListener("click", () => showActNowPreviewFile(files, i));
      actNowFilesEl.appendChild(btn);
    });
  }
  showActNowPreviewFile(files, 0);
  backBtn.classList.add("hidden"); // this is a confirm/discard state, not a Step-Into peek
  actNowRefineInput.value = "";
  endActNowRefine();
  actNowConfirmRow.classList.remove("hidden");
}

// A refinement that undid the whole proposal: nothing is left to apply, and
// the server has already dropped the pending preview.
export function onActNowCleared(payload) {
  clearThinking();
  endActNowRefine();
  actNowConfirmRow.classList.add("hidden");
  backToHunk();
  showNotice(payload.message, "info");
}

// An Act Now or refine run was cancelled server-side (Interrupt, or any
// other action that cancels) before it produced a preview. Undo what the
// request locked, as a failure would (showError), but leave alone the
// bubble and buttons of whatever action did the cancelling.
export function onActNowStopped(payload) {
  if (!clearThinkingIf("act-now-pending") && !state.actNowPending && !state.actNowRefining) return;
  if (payload.kind === "refine_act_now") {
    // The preview being refined is still the pending one, so unlock its bar.
    if (state.actNowRefining) endActNowRefine();
  } else if (state.actNowPending) {
    state.actNowPending = false;
    setActNowActive(true);
  }
  showNotice(payload.message, "info", { clearPending: false });
}

function showActNowPreviewFile(files, index) {
  const file = files[index];
  hunkMetaEl.replaceChildren(makeIconSpan("zap"), document.createTextNode(` Act Now preview — ${file.file_path}`));
  renderCodeView(file.full_lines, file.highlight_start, file.highlight_end, file.file_path);
  actNowFilesEl.querySelectorAll("button").forEach((btn, i) => btn.setAttribute("aria-pressed", String(i === index)));
}

actNowApplyBtn.addEventListener("click", () => {
  actNowConfirmRow.classList.add("hidden");
  send("confirm_act_now");
  showThinking(iconHtml("zap") + " applying...");
});
actNowDiscardBtn.addEventListener("click", () => {
  actNowConfirmRow.classList.add("hidden");
  backToHunk();
});

// Refine: the agent re-runs on top of the proposal on screen with this
// follow-up, and its result replaces the preview (handle_refine_act_now).
// The bar stays up but locked meanwhile, so the preview being refined can't
// be applied or discarded under it.
function setActNowBarBusy(busy) {
  actNowRefineInput.disabled = busy;
  actNowApplyBtn.disabled = busy;
  actNowDiscardBtn.disabled = busy;
  actNowRefineBtn.disabled = busy || !actNowRefineInput.value.trim();
}

// Also showError's route back when a refine fails: the previous preview is
// still the pending one, so the bar is unlocked rather than hidden.
export function endActNowRefine() {
  state.actNowRefining = false;
  setActNowBarBusy(false);
}

function refineActNow() {
  const text = actNowRefineInput.value.trim();
  if (!text || state.actNowRefining) return;
  send("refine_act_now", { text });
  state.actNowRefining = true;
  setActNowBarBusy(true);
  showThinking(iconHtml("zap") + ` ${state.actNowStatus.agent || "the agent"} is refining the change...`, "act-now-pending");
}

actNowRefineBtn.addEventListener("click", refineActNow);
actNowRefineInput.addEventListener("input", () => {
  actNowRefineBtn.disabled = state.actNowRefining || !actNowRefineInput.value.trim();
});
actNowRefineInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  refineActNow();
});

export function backToHunk() {
  // Leaving explore mode (if that's where "Back" was pressed from) — File
  // chat's filter target goes back to the real hunk's file rather than
  // staying pinned to whatever was just explored.
  state.exploringFilePath = null;
  if (!state.savedHunkView) {
    // Nothing to go back *to* — a session with no hunks at all, where the
    // preview is the only thing that's ever been on screen. Leaving the
    // preview up would make Back look broken, so exit to the empty state
    // (which routes through setCodeViewMode and cleans the preview up).
    setComposerEnabled(false);
    if (state.codeViewMode === "md-preview") {
      hunkMetaEl.textContent = "";
      showEmptyCodeView("Nothing to show here.");
    }
    return;
  }
  setComposerEnabled(true); // a real hunk is back on screen — see setComposerEnabled's docstring
  hunkMetaEl.textContent = state.savedHunkView.hunkMetaText;
  state.currentFilePath = state.savedHunkView.filePath;
  applyChatTabFilter();
  renderCodeView(state.savedHunkView.fullLines, state.savedHunkView.highlightStart, state.savedHunkView.highlightEnd, state.savedHunkView.filePath);
  backBtn.classList.add("hidden");
  // Undo onFileExplore's unconditional lock — back on a real hunk, this
  // reverts to the normal review_started-driven rule.
  codeViewEl.classList.toggle("comments-locked", !state.lastKnownReviewStarted);
  // Same reasoning as the renderFilteredFileList() call in onFileExplore:
  // state.currentFilePath just changed back to the real hunk's file and nothing
  // else here would refresh the sidebar's active-pill highlight.
  renderFilteredFileList();
  // backToHunk doesn't go through onPresenting, so without this the row
  // would keep showing file-level chips after leaving explore mode.
  renderPromptSuggestions();
}

// Render the whole file as a diff, with this hunk's changed lines
// highlighted and scrolled into view — full_lines/highlight_* come straight
// from diff_service.get_full_file_diff() via the "presenting" payload (or a
// synthetic context-only array from "definition", for Step Into). Dispatches
// to the merged (VS Code style, default) or split (GitHub style) renderer
// per the Merged/Split toggle — see setDiffViewMode. filePath identifies
// what's on screen for the line-marking feature (see toggleLineMark);
// opts.scrollToActive (default true) is set false when re-rendering purely
// because a mark was toggled, so marking a line elsewhere doesn't yank the
// view back to the hunk's highlighted line every time.
// Only ever called with a hardcoded literal (never untrusted server/user
// text) — same safety rule as showThinking()'s innerHTML use. A blank
// bordered/shadowed box read as "broken" rather than "done"; this at
// least tells the reviewer why nothing's here.

export function applyActNowAvailability() {
  actNowBtn.disabled = textInput.disabled || !state.actNowStatus.available || state.reviewEnded;
  actNowBtn.title = state.reviewEnded
    ? "Act Now — the review has ended; reopen it to make changes"
    : state.actNowStatus.available
      ? `Act Now — your next message goes to ${state.actNowStatus.agent} as a change to make (you'll get a preview to confirm first)`
      : `Act Now is unavailable: ${state.actNowStatus.detail}`;
  if ((!state.actNowStatus.available || state.reviewEnded) && state.actNowActive) setActNowActive(false);
  // Buttons already in the transcript were created against the old status.
  for (const button of document.querySelectorAll(".look-deeper-btn")) applyLookDeeperAvailability(button);
}
applyActNowAvailability();
export function setActNowActive(active) {
  state.actNowActive = active;
  actNowBtn.classList.toggle("active", active);
  actNowBtn.setAttribute("aria-pressed", String(active));
  updateReplyModeUI();
  renderPromptSuggestions();
}
function updateReplyModeUI() {
  textInput.placeholder = state.actNowActive ? "Describe the small change to apply now..." : "Type your reply...";
  textInput.classList.toggle("act-now-active", state.actNowActive);
}
