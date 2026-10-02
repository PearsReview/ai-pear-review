
import { send } from "./ws.js";
import { showThinking } from "./transcript.js";

import { setActNowActive } from "./comments.js";
import { openHandoffDialog } from "./handoff.js";

import { DEFAULT_COMMENT_SEVERITY, rerenderCurrentView } from "./code-view.js";

import { state } from "./state.js";

import { toggleMdBlockSelection } from "./md-preview.js";
import {
  clearMarksBtn,
  codeViewEl,
  finishReviewBtn,
  iconHtml,
  makeIconSpan,
  markedLinesChip,
  markedLinesText,
  promptSuggestionsRow,
  sendBtn,
  stepIntoBtn,
  textInput,
} from "./dom.js";

// Static, hardcoded suggestion text for the prompt-suggestions row (see
// renderPromptSuggestions) — no LLM call generates these yet; that's
// future work. Keyed by which context is currently active — see
// renderPromptSuggestions for the precedence order.
const PROMPT_SUGGESTIONS = {
  file: [
    "Explain what this file does",
    "Summarize the changes in this file",
    "Are there any bugs here?",
  ],
  hunk: [
    "Explain this change",
    "Why was this modified?",
    "Suggest a test for this",
  ],
  selection: [
    "Explain this code",
    "Suggest a better approach",
    "Add a test for this selection",
  ],
  // Act Now instructions, not questions — clicking one fills #text-input
  // the same as any other chip (see insertPromptSuggestion), and the
  // reviewer can edit/append before sending. Kept general-purpose: Act Now
  // already scopes the edit to the current hunk/marked lines via
  // where/snippet (see harness_service.build_act_now_prompt), so these don't
  // need to restate that.
  actNow: [
    "Add a docstring to this function",
    "Add type hints",
    "Extract this into a named constant",
    "Add a null/None check",
    "Rename for clarity",
  ],
};
// Everything the reviewer does directly to the code view and the composer:
// marking lines, the inline per-line comment composer, the suggestion chips,
// and the reply box at the bottom of the chat pane.
//
// All of #code-view's delegated listeners live here, in one place. Four
// features share that one element (line marking, inline comments, markdown
// block selection, the per-line mic) and splitting the listeners across
// modules would leave their ordering — and every stopPropagation between
// them — as the only contract holding them together.

// --- Line marking (double-click a line to mark it, up to 2 — see the
// state.markedLines module docstring near its declaration — or, for keyboard
// users, Tab to a line's mark-gutter button and press Enter/Space; both
// paths funnel through toggleMarkOnRow so there's exactly one
// implementation of "which line does this row/lineno pair refer to") ---
function toggleMarkOnRow(rowEl) {
  if (!rowEl || !state.lastRenderedView) return;
  const oldLineno = rowEl.dataset.oldLineno !== undefined ? parseInt(rowEl.dataset.oldLineno, 10) : null;
  const newLineno = rowEl.dataset.newLineno !== undefined ? parseInt(rowEl.dataset.newLineno, 10) : null;
  if (oldLineno === null && newLineno === null) return; // blank split half — nothing to mark
  toggleLineMark({ filePath: state.lastRenderedView.filePath, oldLineno, newLineno, fullLines: state.lastRenderedView.fullLines });
}
codeViewEl.addEventListener("dblclick", (e) => {
  if (state.codeViewMode === "md-preview") {
    // Same gesture, same delegated listener — only what a double-clicked
    // element *means* differs between a diff row and a preview block.
    const blockEl = e.target.closest(".md-block");
    if (blockEl) toggleMdBlockSelection(parseInt(blockEl.dataset.blockIndex, 10));
  } else {
    toggleMarkOnRow(e.target.closest(".split-half, .code-line"));
  }
  // Double-click's native word-selection would otherwise sit visually on
  // top of (and compete with) the marker dot / range highlight.
  window.getSelection()?.removeAllRanges();
  // The mouseup that preceded this dblclick already set state.currentSelection
  // to the double-clicked word (see the mouseup listener above) and
  // enabled Step Into — removeAllRanges() above clears the visible
  // selection but not that state, so without this Step Into would stay
  // silently armed on a word the reviewer can no longer see selected.
  state.currentSelection = "";
  stepIntoBtn.disabled = true;
});

function toggleLineMark(mark) {
  const idx = state.markedLines.findIndex(
    (m) => m.filePath === mark.filePath && m.oldLineno === mark.oldLineno && m.newLineno === mark.newLineno
  );
  if (idx !== -1) {
    state.markedLines.splice(idx, 1); // toggle off
  } else if (state.markedLines.length < 2) {
    state.markedLines.push(mark);
  } else {
    // A 3rd, different line while 2 are already active — simplest
    // unambiguous rule (see plan discussion): start over rather than guess
    // what 3+ marks should mean.
    state.markedLines = [mark];
  }
  updateMarkedLinesUI();
  rerenderCurrentView();
}

// --- Inline review comments: hover "+" opens a composer anchored to the
// current marks (if any) or just the clicked line, GitHub-PR-review style
// (see state.composingComment/state.queuedComments module state and
// buildLineCommentRowHtml). All of this markup is rebuilt on every
// render (see renderMergedCodeView/renderSplitCodeView), so every
// interactive element here is handled via delegation on #code-view,
// exactly like the dblclick mark-toggle listener above — never
// per-element addEventListener, which would be lost on the next render. ---

function openComposerForLine(filePath, oldLineno, newLineno) {
  const markedContext = buildMarkedContext();
  const anchorLines =
    markedContext && markedContext.length
      ? markedContext
      : [
          {
            file_path: filePath,
            old_lineno: oldLineno,
            new_lineno: newLineno,
            text: (state.lastRenderedView.fullLines.find((l) => l.old_lineno === oldLineno && l.new_lineno === newLineno) || {}).text || "",
          },
        ];
  // Marks are consumed into the composer, same "clears on send" semantics
  // buildMarkedContext's other callers (sendTextReply/onBottomMicDone)
  // already apply.
  state.markedLines = [];
  updateMarkedLinesUI();
  const last = anchorLines[anchorLines.length - 1];
  state.composingComment = {
    anchorLines,
    filePath: last.file_path,
    lastOldLineno: last.old_lineno,
    lastNewLineno: last.new_lineno,
    text: "",
    severity: DEFAULT_COMMENT_SEVERITY,
  };
  renderPromptSuggestions();
  rerenderCurrentView();
}

function closeComposer() {
  state.composingComment = null;
  renderPromptSuggestions();
  rerenderCurrentView();
}

// state.composingComment is deliberately kept alive (not nulled) here and in
// submitComposerAudio below, until either onReviewCommentQueued (success)
// or showError (failure) resolves it. Closing immediately on send used to
// discard the reviewer's typed text or recording the moment
// handle_request_change hit any of its error exits (STT down, no hunk
// loaded) — the composer, the anchor selection, and the just-recorded
// audio were all already gone by the time the "error" frame arrived, with
// nothing left to retry from. `submitting` just guards against sending the
// same request twice while the round trip is in flight.
function submitComposerText() {
  if (!state.composingComment || state.composingComment.submitting) return;
  const textarea = codeViewEl.querySelector(".line-comment-composer-textarea");
  const text = (textarea ? textarea.value : "").trim();
  if (!text) return;
  send("request_change", { text, marked_lines: state.composingComment.anchorLines, severity: state.composingComment.severity });
  state.composingComment.submitting = true;
  rerenderCurrentView();
}

export function submitComposerAudio(base64) {
  if (!state.composingComment || state.composingComment.submitting) return;
  send("request_change", {
    audio_base64: base64,
    marked_lines: state.composingComment.anchorLines,
    severity: state.composingComment.severity,
  });
  state.composingComment.submitting = true;
  showThinking(); // STT still takes real server-side time — same waiting cue the old bottom-input path showed
  rerenderCurrentView();
}

codeViewEl.addEventListener("click", (e) => {
  const selectGutter = e.target.closest(".md-select-gutter");
  if (selectGutter) {
    // The keyboard path to block selection — Tab here and press Enter,
    // mirroring how .mark-gutter makes line marking reachable.
    toggleMdBlockSelection(parseInt(selectGutter.dataset.blockIndex, 10));
    return;
  }
  const markBtn = e.target.closest(".mark-gutter");
  if (markBtn) {
    toggleMarkOnRow(markBtn); // same data-old-lineno/data-new-lineno source markGutterAttrs already put on it
    return;
  }
  const addBtn = e.target.closest(".line-comment-add");
  if (addBtn) {
    const oldLineno = addBtn.dataset.oldLineno !== undefined ? parseInt(addBtn.dataset.oldLineno, 10) : null;
    const newLineno = addBtn.dataset.newLineno !== undefined ? parseInt(addBtn.dataset.newLineno, 10) : null;
    if (oldLineno === null && newLineno === null) return; // blank split half — nothing to comment on
    openComposerForLine(state.lastRenderedView.filePath, oldLineno, newLineno);
    return;
  }
  const badgeBtn = e.target.closest(".line-comment-badge");
  if (badgeBtn) {
    const id = parseInt(badgeBtn.closest(".line-comment-card").dataset.commentId, 10);
    if (state.expandedCommentIds.has(id)) state.expandedCommentIds.delete(id);
    else state.expandedCommentIds.add(id);
    rerenderCurrentView();
    return;
  }
  if (e.target.closest(".line-comment-cancel-btn")) {
    closeComposer();
    return;
  }
  if (e.target.closest(".line-comment-send-btn")) {
    submitComposerText();
    return;
  }
  const editBtn = e.target.closest('[data-action="edit"]');
  if (editBtn) {
    state.editingCommentId = parseInt(editBtn.dataset.commentId, 10);
    state.expandedCommentIds.add(state.editingCommentId);
    rerenderCurrentView();
    return;
  }
  const removeBtn = e.target.closest('[data-action="remove"]');
  if (removeBtn) {
    if (!window.confirm("Remove this comment from the review queue?")) return;
    send("remove_review_comment", { id: parseInt(removeBtn.dataset.commentId, 10) });
    return;
  }
  const doneBtn = e.target.closest('[data-action="done"]');
  if (doneBtn) {
    const id = parseInt(doneBtn.dataset.commentId, 10);
    const cardBody = doneBtn.closest(".line-comment-card-body");
    const textarea = cardBody.querySelector(".line-comment-edit-textarea");
    const severitySelect = cardBody.querySelector(".line-comment-edit-severity-select");
    const edited = (textarea ? textarea.value : "").trim();
    const editedSeverity = severitySelect ? severitySelect.value : DEFAULT_COMMENT_SEVERITY;
    const existing = state.queuedComments.find((c) => c.id === id) || {};
    const original = existing.instruction;
    const originalSeverity = existing.severity || DEFAULT_COMMENT_SEVERITY;
    if (edited && (edited !== original || editedSeverity !== originalSeverity)) {
      send("edit_change_request", { id, instruction: edited, severity: editedSeverity });
    } else {
      state.editingCommentId = null;
      rerenderCurrentView();
    }
  }
});

// Keeps an in-progress draft from silently vanishing if something else
// forces a re-render mid-type (e.g. a review_comment_queued ack for a
// different line arriving while typing) — renderComposerHtml seeds the
// textarea's value from state.composingComment.text on every rebuild.
codeViewEl.addEventListener("input", (e) => {
  if (state.composingComment && e.target.classList.contains("line-comment-composer-textarea")) {
    state.composingComment.text = e.target.value;
  }
});

// Composer's severity <select> — same "keep the in-progress draft alive
// across a forced re-render" reasoning as the textarea's "input" listener
// above, tracked separately since a <select>'s own value survives
// innerHTML rebuilds via the "selected" attribute renderComposerHtml
// writes from this each time (see severitySelectHtml), not by itself.
codeViewEl.addEventListener("change", (e) => {
  if (state.composingComment && e.target.classList.contains("line-comment-severity-select")) {
    state.composingComment.severity = e.target.value;
  }
});

// Escape/Ctrl+Enter on the composer textarea — same idea as the bottom
// #text-input's plain Enter-to-send, adapted for a multi-line textarea
// where plain Enter needs to stay newline-insertion.
codeViewEl.addEventListener("keydown", (e) => {
  if (!e.target.classList.contains("line-comment-composer-textarea")) return;
  if (e.key === "Escape") {
    e.preventDefault();
    closeComposer();
  } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    submitComposerText();
  }
});

function updateMarkedLinesUI() {
  if (!state.markedLines.length) {
    markedLinesChip.classList.add("hidden");
    renderPromptSuggestions();
    return;
  }
  markedLinesChip.classList.remove("hidden");
  let message;
  if (state.markedLines.length === 1) {
    const m = state.markedLines[0];
    message = ` line ${m.newLineno ?? m.oldLineno} of ${m.filePath}`;
  } else {
    const [a, b] = state.markedLines;
    if (a.filePath === b.filePath) {
      const lo = Math.min(a.newLineno ?? a.oldLineno, b.newLineno ?? b.oldLineno);
      const hi = Math.max(a.newLineno ?? a.oldLineno, b.newLineno ?? b.oldLineno);
      message = lo === hi
        ? ` line ${lo} of ${a.filePath}`
        : ` lines ${lo}-${hi} of ${a.filePath} (and everything between)`;
    } else {
      message = " 2 lines marked in different files";
    }
  }
  markedLinesText.replaceChildren(makeIconSpan("map-pin"), document.createTextNode(message));
  renderPromptSuggestions();
}

export function clearMarkedLines() {
  state.markedLines = [];
  updateMarkedLinesUI();
  rerenderCurrentView();
}
clearMarksBtn.addEventListener("click", clearMarkedLines);

// Decides which (if any) static suggestion list applies right now and
// redraws #prompt-suggestions-row. Reads module state directly rather than
// taking params, same style as updateMarkedLinesUI, since every call site
// already just mutated one of these variables. Precedence: Act Now beats
// everything else (its presets are instructions, not questions, and apply
// regardless of whether lines happen to be marked); otherwise an active
// marked-lines selection beats the plain file/hunk default, since it's the
// most specific context a reviewer can be in.
export function renderPromptSuggestions() {
  // These composer states either have their own textarea (the inline
  // review-comment composer, not #text-input) or don't fit any defined
  // suggestion list (markdown preview isn't one of the contexts below) —
  // hide rather than show stale/irrelevant chips.
  if (state.composingComment || state.codeViewMode === "md-preview") {
    promptSuggestionsRow.classList.add("hidden");
    promptSuggestionsRow.replaceChildren();
    return;
  }
  let list;
  if (state.actNowActive) list = PROMPT_SUGGESTIONS.actNow;
  else if (state.markedLines.length) list = PROMPT_SUGGESTIONS.selection;
  else if (state.exploringFilePath) list = PROMPT_SUGGESTIONS.file;
  else if (state.currentFilePath) list = PROMPT_SUGGESTIONS.hunk;
  else {
    // Nothing loaded yet (initial connect, or the "all hunks reviewed" end
    // state where onPresenting resets state.currentFilePath to "").
    promptSuggestionsRow.classList.add("hidden");
    promptSuggestionsRow.replaceChildren();
    return;
  }
  promptSuggestionsRow.classList.remove("hidden");
  promptSuggestionsRow.replaceChildren(
    ...list.map((text) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "prompt-chip";
      chip.textContent = text;
      chip.title = "Put this in the message box to edit or send";
      chip.addEventListener("click", () => insertPromptSuggestion(text));
      return chip;
    })
  );
}

// Inserts (not replaces) suggestion text at the cursor in #text-input, so
// a chip click never destroys a reply the reviewer already started typing.
// Deliberately doesn't touch state.markedLines — buildMarkedContext() reads that
// at send time regardless of what's in the box (see sendTextReply).
function insertPromptSuggestion(text) {
  const start = textInput.selectionStart ?? textInput.value.length;
  const end = textInput.selectionEnd ?? textInput.value.length;
  const before = textInput.value.slice(0, start);
  const after = textInput.value.slice(end);
  const needsSpaceBefore = before.length > 0 && !/\s$/.test(before);
  const needsSpaceAfter = after.length > 0 && !/^\s/.test(after);
  const insertion = (needsSpaceBefore ? " " : "") + text + (needsSpaceAfter ? " " : "");
  textInput.value = before + insertion + after;
  const cursorPos = (before + insertion).length;
  textInput.focus();
  textInput.setSelectionRange(cursorPos, cursorPos);
  autoGrowTextInput();
}

// Create plan: callable any time comments are queued — not gated on
// "reviewed everything" (see handle_finish_review). The dialog asks for the
// optional note and the hand-off choice, then sends finish_review itself.
finishReviewBtn.addEventListener("click", () => openHandoffDialog());

// Expands the current state.markedLines into the payload sent to the server: 1
// mark -> that single line; 2 marks in the same file -> every line
// between them inclusive (per the user's "use the code between the 2
// points" rule), each with its own text so the server never has to re-read
// a file that may have changed since (and a marked "del" line may not
// exist in the working tree at all). Returns null if nothing's marked.
export function buildMarkedContext() {
  if (!state.markedLines.length) return null;
  const asPayloadLine = (m) => {
    const full = m.fullLines.find((l) => l.old_lineno === m.oldLineno && l.new_lineno === m.newLineno);
    return {
      file_path: m.filePath,
      old_lineno: m.oldLineno,
      new_lineno: m.newLineno,
      text: full ? full.text : "",
      kind: full ? full.kind : "context",
    };
  };
  if (state.markedLines.length === 1) return [asPayloadLine(state.markedLines[0])];

  const [a, b] = state.markedLines;
  if (a.filePath !== b.filePath) {
    // Shouldn't normally happen (marking a different file's line clears
    // and restarts per toggleLineMark) but defensive: no sensible "between"
    // across two files, fall back to just the two marked lines.
    return [asPayloadLine(a), asPayloadLine(b)];
  }
  const idxA = a.fullLines.findIndex((l) => l.old_lineno === a.oldLineno && l.new_lineno === a.newLineno);
  const idxB = a.fullLines.findIndex((l) => l.old_lineno === b.oldLineno && l.new_lineno === b.newLineno);
  if (idxA === -1 || idxB === -1) return [asPayloadLine(a), asPayloadLine(b)];
  const lo = Math.min(idxA, idxB);
  const hi = Math.max(idxA, idxB);
  return a.fullLines.slice(lo, hi + 1).map((l) => ({
    file_path: a.filePath,
    old_lineno: l.old_lineno,
    new_lineno: l.new_lineno,
    text: l.text,
    kind: l.kind,
  }));
}

export function sendTextReply() {
  const text = textInput.value.trim();
  if (!text) return;
  // Explore mode has no Act Now/marked-lines equivalent — a question
  // about a file that hasn't changed always just routes to explore_reply,
  // regardless of Act Now's state (irrelevant, not consulted, while
  // state.exploringFilePath is set).
  if (state.exploringFilePath) {
    send("explore_reply", { text, file_path: state.exploringFilePath });
    textInput.value = "";
    resetTextInputHeight();
    return;
  }
  const markedContext = buildMarkedContext();
  const payload = markedContext ? { text, marked_lines: markedContext } : { text };
  if (state.actNowActive) {
    send("act_now", payload);
    state.actNowPending = true;
    setActNowActive(false);
    showThinking(
      iconHtml("zap") + ` ${state.actNowStatus.agent || "the agent"} is working on it — this can take a minute...`,
      "act-now-pending"
    );
  } else {
    // No showThinking() here — onHumanTurn shows it once the server echoes
    // this message back, right before the reply itself starts generating,
    // so the ordering in the transcript reads: your message, then thinking.
    send("reply", payload);
  }
  textInput.value = "";
  resetTextInputHeight();
  if (markedContext) clearMarkedLines();
}
sendBtn.addEventListener("click", sendTextReply);

// Auto-grow with content (see #text-input in style.css for the matching
// max-height/overflow-y) — reset to "auto" first so a shrink (deleting
// text) is measured correctly too, not just growth; scrollHeight would
// otherwise only ever report the box's current (possibly already-tall)
// height back to itself.
function autoGrowTextInput() {
  textInput.style.height = "auto";
  textInput.style.height = `${textInput.scrollHeight}px`;
}
function resetTextInputHeight() {
  textInput.style.height = "";
}
textInput.addEventListener("input", autoGrowTextInput);

textInput.addEventListener("keydown", (e) => {
  // isComposing is true for the Enter that commits an IME candidate
  // (Japanese/Chinese/Korean input) — without this guard that keystroke
  // sends the half-converted text instead of committing it. Shift+Enter
  // inserts a real newline (this is a <textarea> now, not a single-line
  // <input>) — preventDefault only on the plain-Enter submit path so the
  // textarea's own default newline-insertion never fires for that case.
  if (e.key === "Enter" && !e.isComposing && !e.shiftKey) {
    e.preventDefault();
    sendTextReply();
  }
});
