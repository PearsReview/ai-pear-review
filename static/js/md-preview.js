// The markdown preview: turning a .md file's parsed blocks into rendered
// DOM, selecting blocks to read aloud, and the bar above the view.
//
// A preview is not full_lines-shaped, so it cannot share renderCodeView
// with the diff renderers. state.codeViewMode records which of the two owns
// #code-view right now, and every path that re-enters a render checks it —
// otherwise a stale hunk gets painted straight over the preview.
//
// Block selection is deliberately separate from state.markedLines: a
// selection here means "read this part aloud" and must never reach an LLM
// prompt. See the note on state.mdSelectedBlocks.

import { state } from "./state.js";
import { send } from "./ws.js";
import { clearError, showNotice } from "./status.js";
import { setCodeViewMode } from "./code-view.js";
import { showBackToHunkBtn } from "./review-flow.js";
import {
  actNowConfirmRow,
  backBtn,
  codePaneEl,
  codeViewEl,
  hunkMetaEl,
  makeIconSpan,
  mdClearSelectionBtn,
  mdCopyInstructionBtn,
  mdCopyPlanBtn,
  mdFollowBtn,
  mdPauseBtn,
  mdPreviewSummary,
  mdReadAllBtn,
  mdReadSelectionBtn,
  mdStopBtn,
} from "./dom.js";
import { fileAudioPaused, fileAudioPlaying, fileAudioQueue, pauseFileAudio, resumeFileAudio } from "./audio.js";
import { renderFilteredFileList } from "./sidebar.js";

// --- Markdown preview rendering -------------------------------------------
// The one renderer in this file that builds DOM nodes instead of an HTML
// string. That's not an inconsistency, it's the point: the server sends
// structured spans (see markdown_speech.py) and every piece of file content
// lands in .textContent, so nothing derived from a file can ever become
// markup. It's the strictest form of the escaping rule the rest of this
// file follows with escapeHtml.

export function onMdPreview(payload) {
  clearError();
  state.composingComment = null; // a half-typed comment belongs to a hunk, not to this
  actNowConfirmRow.classList.add("hidden"); // same stale-preview hazard onPresenting guards
  state.mdPreview = {
    filePath: payload.file_path,
    contentHash: payload.content_hash,
    blocks: payload.blocks || [],
    text: payload.text || "",
  };
  state.mdSelectedBlocks = [];
  setFollowReading(true);
  hunkMetaEl.replaceChildren(makeIconSpan("book-open"), document.createTextNode(` Preview — ${payload.file_path}`));
  // Start at the top: .code-pane is the scroll container and keeps whatever
  // offset the diff left behind, which would otherwise drop the reviewer
  // into the middle of a document they just opened.
  renderMdPreview({ scrollToTop: true });
  showBackToHunkBtn();
  updateMdPreviewBar();
  renderFilteredFileList(); // the pill's preview icon picks up its active state
  // A notice held back until this preview opened (Create plan's "Plan
  // created…"), so the clearError above doesn't swallow it.
  if (state.noticeAfterPreview) {
    showNotice(state.noticeAfterPreview.message, state.noticeAfterPreview.level);
    state.noticeAfterPreview = null;
  }
  updateMdCopyButtons();
}

// Everything the preview owns, released in one place so no exit path can
// forget a piece. Called only from setCodeViewMode.
export function exitMdPreview() {
  state.mdPreview = null;
  state.mdSelectedBlocks = [];
  state.mdReadingChunk = null;
  state.mdReadingBlockIndex = null;
  backBtn.classList.add("hidden");
}

// [min, max] of the selected block indices — 1 selected means just itself,
// 2 means everything between them inclusive. Same rule (and same "start
// over on a 3rd" behavior in toggleMdBlockSelection) as line marking.
export function mdSelectionRange() {
  if (!state.mdSelectedBlocks.length) return null;
  return [Math.min(...state.mdSelectedBlocks), Math.max(...state.mdSelectedBlocks)];
}

function mdSelectionLineRange() {
  const range = mdSelectionRange();
  if (!range || !state.mdPreview) return null;
  return {
    start_line: state.mdPreview.blocks[range[0]].start_line,
    end_line: state.mdPreview.blocks[range[1]].end_line,
  };
}

export function chunkCoversBlock(chunk, blockIndex) {
  return !!chunk && (chunk.blocks || []).some((b) => b.block_index === blockIndex);
}

function makeMdSelectGutter(blockIndex) {
  // A real <button>, mirroring .mark-gutter, so block selection is reachable
  // by keyboard rather than being a mouse-only gesture.
  const selected = state.mdSelectedBlocks.includes(blockIndex);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "md-select-gutter" + (selected ? " selected" : "");
  btn.dataset.blockIndex = String(blockIndex);
  btn.setAttribute("aria-pressed", String(selected));
  btn.setAttribute("aria-label", selected ? "Deselect this block" : "Select this block for reading");
  btn.title = "Double-click a block to select it (up to 2)";
  btn.textContent = "●";
  return btn;
}

function appendSpans(el, spans) {
  for (const span of spans || []) {
    if (!span.text) continue;
    if (span.style === "plain") {
      el.appendChild(document.createTextNode(span.text));
      continue;
    }
    const node = document.createElement(span.style === "code" ? "code" : "span");
    node.className = "md-span md-" + span.style;
    node.textContent = span.text;
    // A title tooltip, never an <a href> — a file-derived URL in an href
    // would be a javascript:-URI injection surface this app doesn't have
    // anywhere else, and navigating away mid-review is never wanted.
    if (span.style === "link" && span.href) node.title = span.href;
    el.appendChild(node);
  }
}

export function appendBlockBody(el, block) {
  if (block.kind === "code") {
    const pre = document.createElement("pre");
    // Not "md-code": that's already the block-kind class on this block's own
    // wrapper (and, scoped as .md-span.md-code, the inline-code style).
    pre.className = "md-code-block";
    const code = document.createElement("code");
    code.textContent = block.code_text || "";
    if (block.code_lang) pre.dataset.lang = block.code_lang;
    pre.appendChild(code);
    el.appendChild(pre);
    return;
  }
  if (block.kind === "rule") return; // drawn with a CSS border
  if (block.kind === "table_row") {
    for (const cell of block.cells || []) {
      const span = document.createElement("span");
      span.className = "md-cell";
      span.textContent = cell;
      el.appendChild(span);
    }
    return;
  }
  if (block.kind === "list_item" && block.ordered && block.marker) {
    // The author's own numbering, not a CSS counter — a list starting at 7
    // should still say 7.
    const marker = document.createElement("span");
    marker.className = "md-list-marker";
    marker.textContent = block.marker;
    el.appendChild(marker);
  }
  appendSpans(el, block.spans);
}

export function renderMdPreview(opts = {}) {
  if (!state.mdPreview) return;
  setCodeViewMode("md-preview");
  // Nothing here is renderCodeView-shaped, and a stale value would let one
  // of rerenderCurrentView's callers redraw a hunk over the top.
  state.lastRenderedView = null;
  const range = mdSelectionRange();
  const frag = document.createDocumentFragment();
  let tableWrap = null;
  let tableGroup = -1;

  state.mdPreview.blocks.forEach((block, i) => {
    const el = document.createElement("div");
    // list_item -> md-list-item: kinds are snake_case on the wire (Python
    // side) but class names here stay hyphenated like every other class in
    // this file.
    const kindClass = "md-" + block.kind.replace(/_/g, "-");
    el.className = "md-block " + kindClass + (block.level ? " md-level-" + block.level : "");
    el.dataset.blockIndex = String(i);
    el.dataset.startLine = String(block.start_line);
    el.dataset.endLine = String(block.end_line);
    if (block.kind === "list_item" && block.level) el.style.paddingLeft = `calc(2.2rem + ${block.level * 1.1}rem)`;
    if (block.kind === "table_row" && block.is_header) el.classList.add("md-header-row");
    if (state.mdSelectedBlocks.includes(i)) el.classList.add("md-selected");
    if (range && i >= range[0] && i <= range[1]) el.classList.add("md-in-range");
    // Reading state is applied from module state here as well as by the
    // targeted fast path in setMdReadingBlock — so a full rebuild always
    // reproduces exactly what the fast path would have shown.
    if (chunkCoversBlock(state.mdReadingChunk, i)) el.classList.add("md-in-chunk");
    if (state.mdReadingBlockIndex === i) el.classList.add("md-reading");
    el.appendChild(makeMdSelectGutter(i));
    appendBlockBody(el, block);

    // Consecutive table rows share a grid wrapper so their columns line up;
    // each row keeps its own data-block-index, so selection and highlighting
    // are unaffected by the grouping.
    if (block.kind === "table_row") {
      if (!tableWrap || block.table_group !== tableGroup) {
        tableWrap = document.createElement("div");
        tableWrap.className = "md-table";
        tableWrap.style.gridTemplateColumns = `repeat(${Math.max(1, (block.cells || []).length)}, auto)`;
        tableGroup = block.table_group;
        frag.appendChild(tableWrap);
      }
      tableWrap.appendChild(el);
      return;
    }
    tableWrap = null;
    tableGroup = -1;
    frag.appendChild(el);
  });

  codeViewEl.replaceChildren(frag);
  if (opts.scrollToTop) codePaneEl.scrollTop = 0;
}

// True if this exact line (identified the same way toggleLineMark keys
// state.markedLines — old_lineno/new_lineno within a given file) currently has an
// active marker.
// --- Markdown block selection ---------------------------------------------

// Same rule as toggleLineMark: toggle off if already picked, add while
// under 2, and a 3rd starts over rather than guessing what 3+ should mean.
// With 2 picked, everything between them is included (see mdSelectionRange).
export function toggleMdBlockSelection(index) {
  if (!state.mdPreview || Number.isNaN(index)) return;
  const at = state.mdSelectedBlocks.indexOf(index);
  if (at !== -1) {
    state.mdSelectedBlocks.splice(at, 1);
  } else if (state.mdSelectedBlocks.length < 2) {
    state.mdSelectedBlocks.push(index);
  } else {
    state.mdSelectedBlocks = [index];
  }
  updateMdPreviewBar();
  renderMdPreview(); // a full rebuild is fine here — this is user-paced, not 4Hz
}

// The single writer for state.speakingFilePath. It drives two separate bits of
// UI — the pill's speaker/stop icon and the preview bar's Stop button — and
// they used to be refreshed at some assignment sites but not others, so the
// preview bar could sit showing the wrong thing depending on which control
// started the read. One setter, both updated, always.
export function setSpeakingFile(filePath) {
  state.speakingFilePath = filePath;
  renderFilteredFileList();
  if (state.codeViewMode === "md-preview") updateMdPreviewBar();
}

export function clearMdSelection() {
  state.mdSelectedBlocks = [];
  updateMdPreviewBar();
  renderMdPreview();
}

export function setFollowReading(on) {
  state.mdFollowReading = on;
  mdFollowBtn.setAttribute("aria-pressed", String(on));
  mdFollowBtn.classList.toggle("active", on);
}

export function updateMdPreviewBar() {
  const range = mdSelectionRange();
  let message;
  if (!range || !state.mdPreview) {
    message = " Nothing selected — Read all reads the whole file";
  } else {
    const first = state.mdPreview.blocks[range[0]];
    const last = state.mdPreview.blocks[range[1]];
    const count = range[1] - range[0] + 1;
    message =
      first.start_line === last.end_line
        ? ` line ${first.start_line} selected`
        : ` lines ${first.start_line}–${last.end_line} selected (${count} block${count === 1 ? "" : "s"})`;
  }
  // Icon through trusted innerHTML, text as a text node — the same split
  // updateMarkedLinesUI uses.
  mdPreviewSummary.replaceChildren(makeIconSpan("book-open"), document.createTextNode(message));
  mdReadSelectionBtn.disabled = !range;
  // Keyed to audio actually being in flight, not to state.speakingFilePath:
  // that clears as soon as the *last chunk is delivered* (see
  // enqueueFileAudioChunk), which is deliberately a little early for the
  // pill icon but would make Stop disappear while the final chunk is still
  // audibly playing — exactly when it's still wanted.
  //
  // Deliberately the FILE queue only — unlike send()'s audioInFlight,
  // which also counts narration. This button stops a file read; narration
  // playing must not make it appear. Two similar-looking booleans, two
  // different answers on purpose.
  const reading = fileAudioPlaying || fileAudioQueue.length > 0;
  mdStopBtn.classList.toggle("hidden", !reading);
  mdPauseBtn.classList.toggle("hidden", !reading);
  // One button, two actions, so the label is what carries the meaning —
  // deliberately not aria-pressed, which would say "Pause, currently on"
  // when what's true is "Resume". Rebuilt with the same trusted-icon +
  // text-node split mdPreviewSummary uses above.
  mdPauseBtn.replaceChildren(
    makeIconSpan(fileAudioPaused ? "play" : "pause"),
    document.createTextNode(fileAudioPaused ? " Resume" : " Pause")
  );
  mdPauseBtn.title = fileAudioPaused ? "Resume reading" : "Pause reading";
}

export function startMdRead(useSelection) {
  if (!state.mdPreview) return;
  const range = useSelection ? mdSelectionLineRange() : null;
  setFollowReading(true);
  setSpeakingFile(state.mdPreview.filePath);
  send("speak_file", range ? { file_path: state.mdPreview.filePath, ...range } : { file_path: state.mdPreview.filePath });
}

// --- Markdown preview bar -------------------------------------------------
mdReadAllBtn.addEventListener("click", () => startMdRead(false));
mdReadSelectionBtn.addEventListener("click", () => startMdRead(true));
mdPauseBtn.addEventListener("click", () => (fileAudioPaused ? resumeFileAudio() : pauseFileAudio()));
mdStopBtn.addEventListener("click", () => send("stop")); // send()'s hook clears the queue and the icon
mdClearSelectionBtn.addEventListener("click", clearMdSelection);
mdFollowBtn.addEventListener("click", () => setFollowReading(!state.mdFollowReading));

// Turning follow-along off when the reviewer scrolls for themselves.
// Deliberately not a "scroll" listener: smooth scrollIntoView fires scroll
// events of its own, so following would switch itself off the first time it
// worked. These are events only a human generates.
const MD_SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"]);
for (const type of ["wheel", "touchmove"]) {
  codePaneEl.addEventListener(
    type,
    () => {
      if (state.codeViewMode === "md-preview" && state.mdFollowReading) setFollowReading(false);
    },
    { passive: true }
  );
}
codePaneEl.addEventListener("keydown", (e) => {
  if (state.codeViewMode === "md-preview" && state.mdFollowReading && MD_SCROLL_KEYS.has(e.key)) setFollowReading(false);
});

// --- Copy buttons on a review plan -----------------------------------------
//
// Shown only while the preview is a review plan (.review/review_*.md). The
// app never copies unasked; these are how the reviewer takes the hand-off
// elsewhere — the short instruction for their agent, or the whole plan for
// somewhere that can't read the repo.

const REVIEW_PLAN_PATH = /^\.review\/review_[^/]+\.md$/;

export function updateMdCopyButtons() {
  const isPlan = !!state.mdPreview && REVIEW_PLAN_PATH.test(state.mdPreview.filePath);
  mdCopyInstructionBtn.classList.toggle("hidden", !isPlan);
  mdCopyPlanBtn.classList.toggle("hidden", !isPlan);
}

function planInstruction(planFile) {
  // The server's own wording for the plan just created (it knows whether the
  // skill was saved); any older plan gets the file-reading form.
  if (state.handoff && state.handoff.planFile === planFile) return state.handoff.instruction;
  return `Read ${planFile} and follow its instructions`;
}

async function copyWithFeedback(button, text) {
  const original = [...button.childNodes];
  let label;
  try {
    await navigator.clipboard.writeText(text);
    label = " Copied";
  } catch {
    label = " Couldn't copy — select the text instead";
  }
  button.replaceChildren(makeIconSpan("clipboard-check"), document.createTextNode(label));
  setTimeout(() => button.replaceChildren(...original), 2500);
}

mdCopyInstructionBtn.addEventListener("click", () => {
  if (state.mdPreview) copyWithFeedback(mdCopyInstructionBtn, planInstruction(state.mdPreview.filePath));
});
mdCopyPlanBtn.addEventListener("click", () => {
  if (state.mdPreview) copyWithFeedback(mdCopyPlanBtn, state.mdPreview.text);
});
