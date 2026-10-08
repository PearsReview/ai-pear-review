// Every DOM element the UI reaches for by id, resolved once at import time,
// plus the two helpers that build trusted markup.
//
// Safe to resolve at import time because index.html loads this as a module:
// module scripts are deferred, so the document is fully parsed before any of
// this runs. A classic <script> in <head> would get nulls here.
//
// Only elements more than one module touches belong in this file. A control
// only its own feature uses (the settings panel's inputs, the tour's card)
// is looked up in that module instead.

export const transcriptEl = document.getElementById("transcript");
export const hunkMetaEl = document.getElementById("hunk-meta");
export const codeViewEl = document.getElementById("code-view");
export const statusBarEl = document.getElementById("status-bar");
export const errorBannerEl = document.getElementById("error-banner");
export const textInput = document.getElementById("text-input");
export const micBtn = document.getElementById("mic-btn");
export const sendBtn = document.getElementById("send-btn");
export const audioEl = document.getElementById("tts-audio");
export const reviewedBtn = document.getElementById("reviewed-btn");
export const reviewProgressEl = document.getElementById("review-progress");
export const stepIntoBtn = document.getElementById("step-into-btn");
export const nextBtn = document.getElementById("next-btn");
export const prevBtn = document.getElementById("prev-btn");
export const backBtn = document.getElementById("back-btn");
export const interruptBtn = document.getElementById("interrupt-btn");
export const refreshDiffBtn = document.getElementById("refresh-diff-btn");
export const backToSummaryBtn = document.getElementById("back-to-summary-btn");
export const reopenReviewBtn = document.getElementById("reopen-review-btn");
export const actNowBtn = document.getElementById("act-now-btn");
export const startReviewBtn = document.getElementById("start-review-btn");
export const endReviewBtn = document.getElementById("end-review-btn");
export const reviewAllToggleEl = document.getElementById("review-all-toggle");
export const actNowConfirmRow = document.getElementById("act-now-confirm-row");
export const actNowApplyBtn = document.getElementById("act-now-apply-btn");
export const actNowDiscardBtn = document.getElementById("act-now-discard-btn");
export const actNowRefineBtn = document.getElementById("act-now-refine-btn");
export const actNowRefineInput = document.getElementById("act-now-refine-input");
export const actNowSummaryEl = document.getElementById("act-now-summary");
export const actNowFilesEl = document.getElementById("act-now-files");
export const fileListEl = document.getElementById("file-list");
export const fileListItemsEl = document.getElementById("file-list-items");
export const fileFilterInput = document.getElementById("file-filter-input");
export const fileSidebarEl = document.getElementById("file-sidebar");
export const fileSidebarTab = document.getElementById("file-sidebar-tab");
export const fileSidebarTabIcon = document.getElementById("file-sidebar-tab-icon");
export const chatPaneEl = document.getElementById("chat-pane");
export const chatPaneContentEl = document.getElementById("chat-pane-content");
export const chatPaneTab = document.getElementById("chat-pane-tab");
export const chatPaneTabIcon = document.getElementById("chat-pane-tab-icon");
export const reviewedBtnIcon = document.getElementById("reviewed-btn-icon");
export const reviewedBtnLabel = document.getElementById("reviewed-btn-label");
export const filesMenuLabel = document.getElementById("files-menu-label");
export const reviewAllIcon = document.getElementById("review-all-icon");
export const codePaneEl = document.getElementById("code-pane");
export const viewModeToggleEl = document.getElementById("view-mode-toggle");
export const mdPreviewBar = document.getElementById("md-preview-bar");
export const mdPreviewSummary = document.getElementById("md-preview-summary");
export const mdFollowBtn = document.getElementById("md-follow-btn");
export const mdClearSelectionBtn = document.getElementById("md-clear-selection-btn");
export const mdReadSelectionBtn = document.getElementById("md-read-selection-btn");
export const mdReadAllBtn = document.getElementById("md-read-all-btn");
export const mdPauseBtn = document.getElementById("md-pause-btn");
export const mdStopBtn = document.getElementById("md-stop-btn");
export const markedLinesChip = document.getElementById("marked-lines-chip");
export const markedLinesText = document.getElementById("marked-lines-text");
export const reviewQueueRow = document.getElementById("review-queue-row");
export const reviewQueueCount = document.getElementById("review-queue-count");
export const finishReviewBtn = document.getElementById("finish-review-btn");
export const viewPlanBtn = document.getElementById("view-plan-btn");
export const mdCopyInstructionBtn = document.getElementById("md-copy-instruction-btn");
export const mdCopyPlanBtn = document.getElementById("md-copy-plan-btn");
export const handoffDialog = document.getElementById("handoff-dialog");
export const handoffIntro = document.getElementById("handoff-intro");
export const handoffNote = document.getElementById("handoff-note");
export const handoffCreateBtn = document.getElementById("handoff-create-btn");
export const handoffEndOnlyBtn = document.getElementById("handoff-end-only-btn");
export const handoffCancelBtn = document.getElementById("handoff-cancel-btn");
export const clearMarksBtn = document.getElementById("clear-marks-btn");
export const exploreAllFilesBtn = document.getElementById("explore-all-files-btn");
export const promptSuggestionsRow = document.getElementById("prompt-suggestions-row");

// Icon sprite helpers (see the <symbol> definitions inlined at the top of
// index.html). iconHtml() is only ever called with a hardcoded, trusted
// icon name literal — never with anything derived from server/user data —
// so string-building its tiny <svg><use> markup is safe. makeIconSpan()
// wraps that in a <span> for call sites that need to mix an icon with
// *untrusted* dynamic text (file paths, instructions, etc.): the icon
// portion goes through the same trusted-literal innerHTML, and callers are
// expected to append the untrusted part as a separate text node (safe,
// auto-escaping) rather than string-concatenating it into innerHTML too.
export function iconHtml(name) {
  return `<svg class="icon"><use href="#icon-${name}"></use></svg>`;
}
export function makeIconSpan(name) {
  const span = document.createElement("span");
  span.innerHTML = iconHtml(name);
  return span;
}

export function escapeHtml(text) {
  // Quotes matter here, not just <>&: this is also used inside double-
  // quoted title=/aria-label= attributes (e.g. the inline-comment badge's
  // title, built from a server-echoed file_path that can itself contain a
  // literal ". Omitting them let a crafted path break out of the
  // attribute and inject markup.
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
