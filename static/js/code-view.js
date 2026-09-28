
import { state } from "./state.js";
import { exitMdPreview } from "./md-preview.js";
import {
  codeViewEl,
  escapeHtml,
  iconHtml,
  mdPreviewBar,
  viewModeToggleEl,
} from "./dom.js";

// Triage tags for review comments, mirrors server.py's
// COMMENT_SEVERITIES/DEFAULT_COMMENT_SEVERITY. No "question" tag — that
// only earns its keep when a resolve step reads a thread and replies with
// an investigated answer (Diffity's /diffity-resolve does this); without
// that PR-review-integration shape (deliberately out of scope here) it'd
// just be a label nothing ever answers. Order here is the order options
// appear in both the composer's and the edit form's <select>.
export const COMMENT_SEVERITIES = [
  { value: "must-fix", label: "Must fix" },
  { value: "suggestion", label: "Suggestion" },
  { value: "nit", label: "Nit" },
];
export const DEFAULT_COMMENT_SEVERITY = "suggestion";
// Rendering the diff into #code-view: merged and split layouts, the line
// gutters that carry mark and add-comment affordances, and the inline
// comment cards/composer drawn between code rows.
//
// A full rebuild every time, with no DOM diffing — marks, highlights and
// queued comments are all read at render time, so there is exactly one way
// a row can end up on screen. rerenderCurrentView is the single re-entry
// point everything else calls.

export function showEmptyCodeView(html) {
  setCodeViewMode("diff");
  viewModeToggleEl.classList.add("hidden"); // nothing on screen to merge/split either
  codeViewEl.innerHTML = `<div class="code-view-empty">${html}</div>`;
}

// The single place #code-view changes hands between the diff renderers and
// the markdown preview. Routing every transition through here is what makes
// cleanup impossible to forget: renderCodeView and showEmptyCodeView both
// call it, and every way out of the preview (Next/Prev/jump/Step Into/Act
// Now/Back) ends in one of those two.
export function setCodeViewMode(mode) {
  if (state.codeViewMode === "md-preview" && mode !== "md-preview") exitMdPreview();
  state.codeViewMode = mode;
  codeViewEl.classList.toggle("md-preview-mode", mode === "md-preview");
  // Merged/Split are meaningless for a preview — hide rather than leave two
  // dead buttons that silently do nothing.
  viewModeToggleEl.classList.toggle("hidden", mode === "md-preview");
  mdPreviewBar.classList.toggle("hidden", mode !== "md-preview");
}

export function renderCodeView(fullLines, highlightStart, highlightEnd, filePath, opts = {}) {
  setCodeViewMode("diff");
  const scrollToActive = opts.scrollToActive !== false;
  state.lastRenderedView = { fullLines, highlightStart, highlightEnd, filePath };
  if (!fullLines || !fullLines.length) {
    showEmptyCodeView("Nothing to show here.");
    return;
  }
  // Merged/Split only mean anything when there's an actual diff to lay out
  // two ways — an unchanged file (explore mode) or a Step Into peek (see
  // onFileExplore/onDefinition) is all "context" lines, nothing add/del,
  // so both renderers would draw the same plain listing anyway. Hide
  // rather than leave a live toggle that changes nothing.
  const hasChanges = fullLines.some((line) => line.kind === "add" || line.kind === "del");
  viewModeToggleEl.classList.toggle("hidden", !hasChanges);
  if (state.diffViewMode === "split") {
    renderSplitCodeView(fullLines, highlightStart, highlightEnd, filePath, scrollToActive);
  } else {
    renderMergedCodeView(fullLines, highlightStart, highlightEnd, filePath, scrollToActive);
  }
}

// Re-renders from the last renderCodeView() call's own args, without a
// server round-trip and without yanking scroll position back to the
// highlighted hunk line (scrollToActive: false) — used any time purely
// client-side state that renderCodeView reads (marks, inline comment
// composer/badges) changes and the view needs to reflect it immediately.
export function rerenderCurrentView() {
  // The markdown preview owns #code-view and isn't renderCodeView-shaped;
  // redrawing a diff here would paint a stale hunk straight over it.
  // (state.lastRenderedView is also nulled while previewing, which would make
  // this return anyway — this is the explicit version of that, so the
  // intent doesn't rest on a coincidence.)
  if (state.codeViewMode === "md-preview") return;
  if (!state.lastRenderedView) return;
  renderCodeView(
    state.lastRenderedView.fullLines,
    state.lastRenderedView.highlightStart,
    state.lastRenderedView.highlightEnd,
    state.lastRenderedView.filePath,
    { scrollToActive: false }
  );
}

function isLineMarked(filePath, oldLineno, newLineno) {
  return state.markedLines.some((m) => m.filePath === filePath && m.oldLineno === oldLineno && m.newLineno === newLineno);
}

// With exactly 2 marks in this file, returns the [min, max] fullLines
// index range between them (inclusive) — null otherwise (0 or 1 marks, or
// the 2 marks aren't in this file, or one can't be located in this
// particular fullLines array, e.g. a stale reference after a refresh).
function markRangeIndices(fullLines, filePath) {
  const inFile = state.markedLines.filter((m) => m.filePath === filePath);
  if (inFile.length !== 2) return null;
  const indices = inFile.map((m) =>
    fullLines.findIndex((l) => l.old_lineno === m.oldLineno && l.new_lineno === m.newLineno)
  );
  if (indices.includes(-1)) return null;
  return [Math.min(...indices), Math.max(...indices)];
}

function markGutterAttrs(line) {
  const oldAttr = line.old_lineno !== null && line.old_lineno !== undefined ? ` data-old-lineno="${line.old_lineno}"` : "";
  const newAttr = line.new_lineno !== null && line.new_lineno !== undefined ? ` data-new-lineno="${line.new_lineno}"` : "";
  return oldAttr + newAttr;
}

// Hover-reveal "+" button (see .line-comment-add in style.css — opacity 0
// at rest, revealed by :hover on the row/half) that opens the inline
// comment composer for this exact line. Rendered on every line
// unconditionally rather than only where useful — CSS hover is what makes
// it "appear", not conditional markup.
// A real <button> (not a <span>) — Tab-reachable and Enter/Space-
// activatable via native button semantics, wired to the same
// toggleLineMark path the dblclick gesture uses (see toggleMarkOnRow).
// Hidden at rest and revealed on hover/focus (style.css) rather than only
// discoverable via the title tooltip, matching .line-comment-add's own
// hover-reveal treatment.
function markGutterButtonHtml(oldLineno, newLineno, marked) {
  const label = marked ? "Unmark this line" : "Mark this line";
  return (
    `<button class="mark-gutter${marked ? " marked" : ""}" type="button"` +
    `${markGutterAttrs({ old_lineno: oldLineno, new_lineno: newLineno })} title="Double-click a line to mark it (up to 2)" aria-label="${label}">●</button>`
  );
}

function lineCommentAddButtonHtml(oldLineno, newLineno) {
  // Same null-omission convention as markGutterAttrs (rather than always
  // including the attribute, possibly empty) — so the click handler can
  // tell "this line genuinely has no old/new lineno" apart from "the
  // attribute happens to be an empty string" using the same
  // dataset.x !== undefined check the dblclick mark-toggle handler uses.
  return `<button class="line-comment-add" type="button"${markGutterAttrs({ old_lineno: oldLineno, new_lineno: newLineno })} title="Add review comment" aria-label="Add review comment">${iconHtml("message-square-plus")}</button>`;
}

// --- Inline review comments anchored to a code line (see state.composingComment/
// state.queuedComments module state, and server.py's _line_context/"anchor") ---

function findQueuedCommentsForLine(filePath, oldLineno, newLineno) {
  return state.queuedComments.filter(
    (c) => c.file_path === filePath && c.anchor && c.anchor.last_old_lineno === oldLineno && c.anchor.last_new_lineno === newLineno
  );
}

function isComposingForLine(filePath, oldLineno, newLineno) {
  return (
    !!state.composingComment &&
    state.composingComment.filePath === filePath &&
    state.composingComment.lastOldLineno === oldLineno &&
    state.composingComment.lastNewLineno === newLineno
  );
}

// One full-width row, inserted as a sibling right after a line's own row
// markup (both renderMergedCodeView and renderSplitCodeView call this the
// same way) — holds 0+ queued-comment cards for this line plus the
// composer if one's currently open here. Empty string (no extra row) when
// neither applies, which is the common case for most lines.
function buildLineCommentRowHtml(filePath, oldLineno, newLineno) {
  const parts = findQueuedCommentsForLine(filePath, oldLineno, newLineno).map(renderQueuedCommentCardHtml);
  if (isComposingForLine(filePath, oldLineno, newLineno)) parts.push(renderComposerHtml());
  return parts.length ? `<div class="line-comment-row">${parts.join("")}</div>` : "";
}

// Shared <select> markup for both the composer (new comment) and the
// edit form (existing comment) — options come from COMMENT_SEVERITIES so
// the two never drift apart. Rebuilt on every render like everything else
// in this section; the change listener below reads the selected value
// back off the DOM rather than this function keeping any state itself.
function severitySelectHtml(className, selectedValue) {
  const options = COMMENT_SEVERITIES.map(
    (s) => `<option value="${s.value}" ${s.value === selectedValue ? "selected" : ""}>${escapeHtml(s.label)}</option>`
  ).join("");
  return `<select class="${className}" aria-label="Comment severity">${options}</select>`;
}

function severityLabel(value) {
  return (COMMENT_SEVERITIES.find((s) => s.value === value) || {}).label || value;
}

export function renderComposerHtml() {
  const submitting = !!state.composingComment.submitting;
  const micDisabledAttr = !state.sttAvailable || !state.sttPrefOn || submitting ? "disabled" : "";
  const sendDisabledAttr = submitting ? "disabled" : "";
  const first = state.composingComment.anchorLines[0];
  const firstNo = first.new_lineno ?? first.old_lineno;
  const lastNo = state.composingComment.lastNewLineno ?? state.composingComment.lastOldLineno;
  const label = state.composingComment.anchorLines.length > 1 && firstNo !== lastNo ? `lines ${firstNo}-${lastNo}` : `line ${lastNo}`;
  return (
    `<div class="line-comment-composer">` +
    `<div class="line-comment-composer-header">${iconHtml("map-pin")} Commenting on ${escapeHtml(label)} of ${escapeHtml(state.composingComment.filePath)}</div>` +
    `<textarea class="edit-request-textarea line-comment-composer-textarea" placeholder="Describe the change to request..." title="Ctrl+Enter to submit, Esc to cancel" aria-label="Review comment text">${escapeHtml(state.composingComment.text || "")}</textarea>` +
    `<div class="line-comment-composer-actions">` +
    severitySelectHtml("line-comment-severity-select", state.composingComment.severity || DEFAULT_COMMENT_SEVERITY) +
    `<button class="line-comment-mic-btn" type="button" ${micDisabledAttr} title="Push to talk" aria-label="Push to talk">${iconHtml("mic")}</button>` +
    `<button class="line-comment-cancel-btn" type="button" title="Discard this comment (Esc)">${iconHtml("x")} Cancel</button>` +
    `<button class="line-comment-send-btn" type="button" title="Add this comment to the plan (Ctrl+Enter)" ${sendDisabledAttr}>${iconHtml("check")} ${submitting ? "Sending..." : "Comment"}</button>` +
    `</div></div>`
  );
}

function renderQueuedCommentCardHtml(c) {
  const expanded = state.expandedCommentIds.has(c.id);
  const editing = state.editingCommentId === c.id;
  const severity = c.severity || DEFAULT_COMMENT_SEVERITY;
  const severityLabelText = severityLabel(severity);
  const bodyHtml = editing
    ? `<textarea class="edit-request-textarea line-comment-edit-textarea">${escapeHtml(c.instruction)}</textarea>` +
      severitySelectHtml("line-comment-edit-severity-select", severity) +
      `<button class="edit-request-confirm" type="button" title="Save the edited comment" data-action="done" data-comment-id="${c.id}">${iconHtml("check")} Done</button>`
    : `<div class="comment-body">${escapeHtml(c.instruction)}</div>` +
      `<button class="edit-request-btn" type="button" title="Edit this comment" data-action="edit" data-comment-id="${c.id}">${iconHtml("pencil")} Edit</button>` +
      `<button class="edit-request-btn" type="button" title="Remove this comment from the queue" data-action="remove" data-comment-id="${c.id}">${iconHtml("x")} Remove</button>`;
  return (
    `<div class="line-comment-card" data-comment-id="${c.id}">` +
    `<button class="line-comment-badge severity-${severity}" type="button" title="[${escapeHtml(severityLabelText)}] ${escapeHtml(c.where)} — ${escapeHtml(c.file_path)}" aria-label="${escapeHtml(severityLabelText)} comment on ${escapeHtml(c.where)} — ${escapeHtml(c.file_path)}" aria-expanded="${expanded}">${iconHtml("message-square")}</button>` +
    `<div class="line-comment-card-body ${expanded ? "" : "hidden"}">` +
    `<div class="line-comment-card-header">${iconHtml("wrench")} <span class="severity-tag severity-${severity}">${escapeHtml(severityLabelText)}</span> ${escapeHtml(c.where)} — ${escapeHtml(c.file_path)}</div>` +
    bodyHtml +
    `</div></div>`
  );
}

function renderMergedCodeView(fullLines, highlightStart, highlightEnd, filePath, scrollToActive) {
  const range = markRangeIndices(fullLines, filePath);
  const rows = fullLines.map((line, i) => {
    const marker = line.kind === "add" ? "+" : line.kind === "del" ? "-" : "";
    const isHighlighted = highlightStart >= 0 && i >= highlightStart && i <= highlightEnd;
    const inMarkRange = range && i >= range[0] && i <= range[1];
    const marked = isLineMarked(filePath, line.old_lineno, line.new_lineno);
    const classes = ["code-line", line.kind, isHighlighted ? "highlight" : "", inMarkRange ? "mark-range" : ""]
      .filter(Boolean)
      .join(" ");
    const idAttr = i === highlightStart ? ' id="active-hunk-line"' : "";
    return (
      `<div class="${classes}"${idAttr}${markGutterAttrs(line)}>` +
      markGutterButtonHtml(line.old_lineno, line.new_lineno, marked) +
      `<span class="lineno">${line.old_lineno ?? ""}</span>` +
      `<span class="lineno">${line.new_lineno ?? ""}</span>` +
      `<span class="marker">${marker}</span>` +
      lineCommentAddButtonHtml(line.old_lineno, line.new_lineno) +
      `<span class="text">${escapeHtml(line.text)}</span>` +
      `</div>` +
      buildLineCommentRowHtml(filePath, line.old_lineno, line.new_lineno)
    );
  });
  codeViewEl.innerHTML = rows.join("");
  if (scrollToActive) {
    const active = document.getElementById("active-hunk-line");
    if (active) active.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// Groups the flat fullLines sequence into side-by-side rows: a context line
// pairs with itself on both sides; a run of consecutive del lines pairs
// row-by-row with the run of add lines immediately following it (GitHub's
// split-view convention), with a blank half wherever one run is longer than
// the other or empty. leftIndex/rightIndex keep the original fullLines
// index so highlighting/scroll-to-active still lines up with
// highlightStart/highlightEnd, which are indices into that original array.
function buildSplitRows(fullLines) {
  const rows = [];
  let i = 0;
  while (i < fullLines.length) {
    const line = fullLines[i];
    if (line.kind === "context") {
      rows.push({ left: line, right: line, leftIndex: i, rightIndex: i });
      i++;
      continue;
    }
    const dels = [];
    const delIndices = [];
    while (i < fullLines.length && fullLines[i].kind === "del") {
      dels.push(fullLines[i]);
      delIndices.push(i);
      i++;
    }
    const adds = [];
    const addIndices = [];
    while (i < fullLines.length && fullLines[i].kind === "add") {
      adds.push(fullLines[i]);
      addIndices.push(i);
      i++;
    }
    const max = Math.max(dels.length, adds.length);
    for (let k = 0; k < max; k++) {
      rows.push({
        left: dels[k] || null,
        right: adds[k] || null,
        leftIndex: k < delIndices.length ? delIndices[k] : null,
        rightIndex: k < addIndices.length ? addIndices[k] : null,
      });
    }
  }
  return rows;
}

function renderSplitCodeView(fullLines, highlightStart, highlightEnd, filePath, scrollToActive) {
  const rows = buildSplitRows(fullLines);
  const isHighlighted = (idx) => idx !== null && highlightStart >= 0 && idx >= highlightStart && idx <= highlightEnd;
  const range = markRangeIndices(fullLines, filePath);
  const inRange = (idx) => idx !== null && range && idx >= range[0] && idx <= range[1];
  let activeIdSet = false;

  const renderHalf = (line, side) => {
    const kindClass = line ? `${line.kind}-row` : "blank-row";
    const marker = line ? (line.kind === "add" ? "+" : line.kind === "del" ? "-" : "") : "";
    const lineno = line ? (side === "left" ? line.old_lineno : line.new_lineno) : "";
    const text = line ? escapeHtml(line.text) : "";
    const marked = line ? isLineMarked(filePath, line.old_lineno, line.new_lineno) : false;
    const attrs = line ? markGutterAttrs(line) : "";
    return (
      `<div class="split-half split-${side === "left" ? "old" : "new"} ${kindClass}"${attrs}>` +
      (line ? markGutterButtonHtml(line.old_lineno, line.new_lineno, marked) : `<span class="mark-gutter"></span>`) +
      `<span class="lineno">${lineno ?? ""}</span>` +
      `<span class="marker">${marker}</span>` +
      (line ? lineCommentAddButtonHtml(line.old_lineno, line.new_lineno) : `<span class="line-comment-add"></span>`) +
      `<span class="text">${text}</span>` +
      `</div>`
    );
  };

  const html = rows.map((row) => {
    const rowHighlighted = isHighlighted(row.leftIndex) || isHighlighted(row.rightIndex);
    const rowInRange = inRange(row.leftIndex) || inRange(row.rightIndex);
    let idAttr = "";
    if (!activeIdSet && (row.leftIndex === highlightStart || row.rightIndex === highlightStart)) {
      idAttr = ' id="active-hunk-line"';
      activeIdSet = true;
    }
    const classes = ["code-line", "split", rowHighlighted ? "highlight" : "", rowInRange ? "mark-range" : ""]
      .filter(Boolean)
      .join(" ");
    // Resolve against the underlying fullLines entry (not either rendered
    // half) for comment-row matching — same precedent as isLineMarked/
    // inRange above, which also key off the real line, not a half.
    const line = fullLines[row.leftIndex ?? row.rightIndex];
    const commentRowHtml = line ? buildLineCommentRowHtml(filePath, line.old_lineno, line.new_lineno) : "";
    return `<div class="${classes}"${idAttr}>${renderHalf(row.left, "left")}${renderHalf(row.right, "right")}</div>${commentRowHtml}`;
  });

  codeViewEl.innerHTML = html.join("");
  if (scrollToActive) {
    const active = document.getElementById("active-hunk-line");
    if (active) active.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

// The reviewer's own turn (typed or transcribed) now arrives separately and
// earlier, via "human_turn" (see onHumanTurn) — by the time this fires, that
// bubble is already showing, so only the agent's reply is left to append.
