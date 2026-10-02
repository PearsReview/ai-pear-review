
import { clearError } from "./status.js";
let thinkingEl = null; // the transient "…thinking" transcript bubble, if shown
let thinkingTimer = null; // the Look deeper bubble's elapsed-time ticker
import { send } from "./ws.js";
import { state } from "./state.js";
import { appendBlockBody } from "./md-preview.js";
import { iconHtml, transcriptEl } from "./dom.js";
import { speakTurn } from "./audio.js";
import { updateExplainBtn } from "./explain.js";

let lastLabeledHunkIndex = null; // last hunk a transcript divider was shown for
// The chat transcript: reviewer and presenter turns, the "look deeper"
// follow-up button, hunk dividers, and the Overall/File tab filter.
//
// Turns are appended, never re-rendered, so the tab filter works by toggling
// a class on existing bubbles rather than rebuilding the list.

export function onReviewerTurn(payload) {
  clearError();
  clearThinking();
  appendTurn("presenter", payload.text, payload);
}

// --- Look deeper -----------------------------------------------------------
// A read-only investigation by the reviewer's own coding agent (see
// handle_look_deeper). It uses the same agent as Act Now, so its availability
// is state.actNowStatus.

// The question each button re-asks: the reviewer's latest question on that
// hunk at the moment the reply arrived (recorded in appendTurn).
const lastQuestionByHunk = new Map();

function lookDeeperButton(index, question) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "look-deeper-btn";
  button.textContent = "Look deeper";
  applyLookDeeperAvailability(button);
  button.addEventListener("click", () => {
    send("look_deeper", question ? { index, question } : { index });
    showDeeperPending();
  });
  return button;
}

// The in-flight bubble for a Look deeper run: a spinner plus an elapsed-time
// counter, since an agent session can run for minutes with nothing else on
// screen changing. Cleared like any thinking bubble (clearThinking).
function showDeeperPending() {
  showThinking(
    '<span class="spinner" aria-hidden="true"></span>' +
    "<span>Looking deeper — your coding agent is reading the repository (read-only). " +
    'This is a whole agent session, so it takes longer than an ordinary reply… <span class="deeper-elapsed"></span></span>',
    "deeper-pending"
  );
  thinkingEl.setAttribute("role", "status");
  const elapsedEl = thinkingEl.querySelector(".deeper-elapsed");
  const started = Date.now();
  const tick = () => {
    const secs = Math.floor((Date.now() - started) / 1000);
    elapsedEl.textContent = `(${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")})`;
  };
  tick();
  thinkingTimer = setInterval(tick, 1000);
}

// Called for every server "error" before showError clears the bubble: if a
// Look deeper run was the thing in flight, the failure also goes into the
// transcript where the reviewer is watching, not only the banner. Also
// "agent_stopped"'s route for a cancelled run, with its own label.
export function failDeeperIfPending(message, title = "Look deeper didn't finish") {
  if (!clearThinkingIf("deeper-pending")) return;
  const div = document.createElement("div");
  div.className = "turn system deeper-error";
  div.setAttribute("role", "alert");
  const label = document.createElement("div");
  label.className = "role";
  label.textContent = title;
  const body = document.createElement("div");
  body.className = "turn-body";
  body.textContent = message;
  div.append(label, body);
  transcriptEl.appendChild(div);
  div.scrollIntoView({ behavior: "smooth", block: "end" });
}

function speakTurnButton(turnEl, spoken) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "replay-btn speak-turn-btn";
  button.innerHTML = iconHtml("volume-2");
  button.title = "Read this reply aloud";
  button.setAttribute("aria-label", "Read this reply aloud");
  button.addEventListener("click", () => speakTurn(turnEl, spoken, send));
  return button;
}

export function applyLookDeeperAvailability(button) {
  button.disabled = !state.actNowStatus.available;
  button.title = state.actNowStatus.available
    ? "Get a better, more thorough answer — your coding agent searches the whole repository and its history (read-only). Takes longer than a normal reply."
    : `Get a better, more thorough answer from your coding agent — not available yet: ${state.actNowStatus.detail}`;
}

export function onDeeperTurn(payload) {
  clearError();
  clearThinking();
  appendTurn("deeper", payload.text, payload);
}

// Sent by the server as soon as the reviewer's message is known as text —
// immediately for typed replies, right after STT transcription finishes for
// voice ones — rather than waiting for the LLM's response too. Shows what
// was actually heard/typed before the (potentially much slower) reply
// arrives, instead of a single silent wait for both at once.
export function onHumanTurn(payload) {
  clearError();
  appendTurn("reviewer", payload.text, payload);
  showThinking();
}

// hunkInfo ({index, total, file_path}) is which hunk this turn is about —
// every turn today is hunk-scoped, but the param stays optional in case a
// future turn type isn't. A long transcript spanning many quickly-visited
// hunks otherwise reads as one undifferentiated wall of text; a divider
// only appears when the hunk actually changes from the previous turn, and
// it's clickable — reuses the same jump_to_hunk message the file-list
// pills send, so a comment several hunks back is one click to return to.
// Reuses appendBlockBody (defined above, for the markdown-file preview) to
// render a persona reply's sanitised blocks into a chat bubble — narration
// and replies get the same markdown handling a previewed file gets
// (real code boxes and inline `code` instead of literal backticks) rather
// than a second renderer. See sanitize_persona_reply's docstring
// (app/utils/markdown_speech.py) for what server-side sanitising already
// removed before these blocks were sent: an opening "Sure, I'll..."
// announcement, and a fenced code block that just re-pastes the diff
// already shown in the code pane.
//
// Deliberately NOT reusing the .md-block wrapper class the file preview
// uses — that class reserves left padding for a block-selection gutter
// this chat context doesn't have, which would misalign every line. The
// content classes it sets via appendBlockBody (.md-code-block, .md-span.md-code,
// .md-bold, etc.) are unscoped in style.css, so they still apply here for
// free.
function appendTurnBlocks(bodyEl, blocks) {
  bodyEl.classList.add("turn-blocks");
  for (const block of blocks) {
    const el = document.createElement("div");
    el.className = "turn-block md-" + block.kind.replace(/_/g, "-");
    appendBlockBody(el, block);
    bodyEl.appendChild(el);
  }
}

// Links from a narration to the other hunks its prep-review briefing says it
// belongs with ("moved to · app/handlers/narration.py"). Only indices the
// server resolved against the live diff arrive here. textContent only —
// file paths and notes are skill-written data, never markup.
function relatedHunksRow(related) {
  const row = document.createElement("div");
  row.className = "related-hunks";
  const label = document.createElement("span");
  label.className = "related-hunks-label";
  label.textContent = "Related:";
  row.appendChild(label);
  for (const r of related) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "related-hunk";
    chip.textContent = `${r.relation.replace(/_/g, " ")} · ${r.file_path}`;
    chip.title = r.note ? `${r.note} — jump to ${r.file_path}` : `Jump to ${r.file_path}`;
    chip.addEventListener("click", () => send("jump_to_hunk", { index: r.index }));
    row.appendChild(chip);
  }
  return row;
}

export function appendTurn(role, text, hunkInfo) {
  if (hunkInfo && hunkInfo.index !== lastLabeledHunkIndex) {
    appendHunkDivider(hunkInfo);
    lastLabeledHunkIndex = hunkInfo.index;
  }
  const div = document.createElement("div");
  div.className = `turn ${role}`;
  // Every turn today is hunk-scoped (hunkInfo optional only for a future
  // turn type that isn't) — tag it with the file it's about so the File
  // chat tab (see applyChatTabFilter) can filter without touching anything
  // else here.
  if (hunkInfo) div.dataset.filePath = hunkInfo.file_path;
  const roleLabel = document.createElement("div");
  roleLabel.className = "role";
  roleLabel.textContent =
    role === "presenter" ? "Author (Claude)" :
    role === "system" ? "System" :
    role === "deeper" ? `Looked deeper · ${hunkInfo.agent} · ${hunkInfo.model || "Cline's default model"} · read-only` :
    "You";
  const body = document.createElement("div");
  // What the read-along highlight searches for the sentence being spoken
  // (audio.js) — the reply only, not its label or buttons.
  body.className = "turn-body";
  // hunkInfo.blocks — the sanitised, render-ready form the server derived
  // from `text` via sanitize_persona_reply — is only ever present on
  // narration/reviewer_turn payloads (see app/server.py's send sites), so
  // "reviewer"/plain "system" turns fall straight through to plain text.
  if (hunkInfo && hunkInfo.blocks && hunkInfo.blocks.length) {
    appendTurnBlocks(body, hunkInfo.blocks);
  } else {
    body.textContent = text;
  }
  div.appendChild(roleLabel);
  div.appendChild(body);
  if (hunkInfo && hunkInfo.related && hunkInfo.related.length) {
    div.appendChild(relatedHunksRow(hunkInfo.related));
  }
  // Read this reply aloud on request — works with voice output off, so the
  // reviewer can leave narration silent and pick which replies to hear.
  // `spoken` is the sanitised text the server would narrate; the raw text
  // stands in if a payload ever lacks it, so the button is always there.
  const spoken = (hunkInfo && hunkInfo.spoken) || text;
  if ((role === "presenter" || role === "deeper") && spoken && spoken.trim()) {
    div.appendChild(speakTurnButton(div, spoken));
  }
  // index -1 is an explore-mode turn: no hunk for Look deeper to investigate.
  if (hunkInfo && hunkInfo.index >= 0) {
    if (role === "reviewer") lastQuestionByHunk.set(hunkInfo.index, text);
    if (role === "presenter") {
      // A reply answers the question just asked on this hunk; a narration
      // (its payload carries narration_available) answers none, and the
      // server substitutes a "look deeper at this change" question.
      const question = "narration_available" in hunkInfo ? null : lastQuestionByHunk.get(hunkInfo.index);
      div.appendChild(lookDeeperButton(hunkInfo.index, question));
    }
  }

  transcriptEl.appendChild(div);
  div.scrollIntoView({ behavior: "smooth", block: "end" });
  // Only "presenter" turns ever get spoken (try_speak is never called for
  // "reviewer"/"system" turns server-side) — remember this bubble so the
  // audio_chunk that may follow moments later is kept for the right
  // turn's speaker button.
  if (role === "presenter" || role === "deeper") state.lastPresenterTurnEl = div;
  applyChatTabFilter();
}

function appendHunkDivider(hunkInfo) {
  // index: -1 marks an explore-mode turn (see handle_explore_reply in
  // handlers/explore.py) — no real hunk to describe or jump_to_hunk to, so this
  // reads as "exploring this file" and re-opens it via explore_file
  // instead.
  const isExplore = hunkInfo.index === -1;
  const divider = document.createElement("button");
  divider.className = "hunk-divider";
  divider.dataset.filePath = hunkInfo.file_path; // see applyChatTabFilter
  divider.textContent = isExplore ? `Exploring ${hunkInfo.file_path}` : `${hunkInfo.file_path} — hunk ${hunkInfo.index + 1}/${hunkInfo.total}`;
  divider.title = isExplore ? `Reopen ${hunkInfo.file_path}` : `Jump back to ${hunkInfo.file_path}`;
  divider.addEventListener("click", () =>
    isExplore ? send("explore_file", { file_path: hunkInfo.file_path }) : send("jump_to_hunk", { index: hunkInfo.index })
  );
  transcriptEl.appendChild(divider);
}

// "Overall chat" (default) shows every turn; "File chat" shows only turns
// tagged with the currently active file (see appendTurn/appendHunkDivider).
// Untagged nodes (the transient "…thinking" placeholder) are always left
// visible — it's about whatever's on screen right now regardless of tab.
let activeChatTab = "overall";
export function applyChatTabFilter() {
  for (const node of transcriptEl.children) {
    const hide = activeChatTab === "file" && node.dataset.filePath !== undefined && node.dataset.filePath !== state.currentFilePath;
    node.classList.toggle("chat-hidden", hide);
  }
}

// Same toggle-button-group interaction as setDiffViewMode's Merged/Split
// pair (see .chat-tabs/.chat-tab in style.css for the matching visual
// treatment) — plain .active bookkeeping, no tablist ARIA role/state
// since this is a view toggle, not a set of separately-labeled panels.
for (const btn of document.querySelectorAll(".chat-tab")) {
  btn.addEventListener("click", () => {
    activeChatTab = btn.dataset.tab;
    for (const other of document.querySelectorAll(".chat-tab")) {
      other.classList.toggle("active", other === btn);
    }
    applyChatTabFilter();
  });
}

// Re-enables every button this file disables-while-in-flight (see the
// click handlers below) — called both from each action's own expected
// response (onPresenting/onDefinition/onReviewFinished) and from
// showError as a catch-all, so a button can never get stuck disabled just
// because the expected response didn't arrive (e.g. Step Into on text
// that isn't a real identifier still needs step-into-btn usable again).

let narratedByIndex = new Map(); // hunk index -> last-shown text
export function onNarration(payload) {
  clearError();
  clearThinking();
  // No setComposerEnabled(true) needed here — onPresenting, which always
  // fires first for this hunk, already enabled it.
  if (payload.index === state.currentHunkIndex) {
    state.hunkExplained = true;
    updateExplainBtn();
  }
  if (narratedByIndex.get(payload.index) === payload.text) return;
  narratedByIndex.set(payload.index, payload.text);
  // narration_available is false when the conversation agent has no
  // ANTHROPIC_API_KEY (degraded mode) — payload.text is then a placeholder
  // explaining that, not the persona talking, so it gets a distinct role.
  appendTurn(payload.narration_available === false ? "system" : "presenter", payload.text, payload);
}

// text is HTML, not plain text — every call site passes a hardcoded
// literal (see iconHtml()'s own trusted-literal-only contract), never
// anything derived from server/user data.
// kind, if given, tags the bubble as belonging to one long-running action,
// so clearThinkingIf can release it without touching anyone else's.
export function showThinking(text = "…thinking", kind = null) {
  clearThinking();
  state.thinking = true;
  updateExplainBtn();
  thinkingEl = document.createElement("div");
  thinkingEl.className = "turn system thinking";
  if (kind) thinkingEl.classList.add(kind);
  thinkingEl.innerHTML = text;
  transcriptEl.appendChild(thinkingEl);
  thinkingEl.scrollIntoView({ behavior: "smooth", block: "end" });
}

// Clears the bubble only if it is still the one `kind` put up — a cancelled
// agent run must not wipe the bubble of the action that cancelled it.
export function clearThinkingIf(kind) {
  if (!thinkingEl || !thinkingEl.classList.contains(kind)) return false;
  clearThinking();
  return true;
}

export function clearThinking() {
  clearInterval(thinkingTimer);
  thinkingTimer = null;
  state.thinking = false;
  updateExplainBtn();
  if (thinkingEl) {
    thinkingEl.remove();
    thinkingEl = null;
  }
}

// Shown in the toolbar (next to "Review all") in place of showThinking()
// when this connection's review session hasn't started yet (see
// "review_started" in present_current_hunk's "presenting" payload) — briefing/
// conversation aren't in flight at all until the reviewer presses this.
// Lives in .controls alongside the other whole-review controls, not in the
// transcript, since it gates the whole session rather than being part of
// the conversation itself.
// Always the same label regardless of which hunk happens to be on screen
// (payload.index/total used to drive a "Resume Review — Hunk N of Total"
// variant here, but that fired from ordinary pre-start browsing just as
// often as a genuine resume, which made the button read inconsistently
// across a review's many files for no real signal). A genuine resume is
// now handled server-side instead — see session_store.py: review_started
// loads from disk, so on a real resume this button never shows at all,
